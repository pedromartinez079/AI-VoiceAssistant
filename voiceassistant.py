import requests
import queue
import os
import threading
import time
import tempfile
import kivy
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.spinner import Spinner
from kivy.clock import mainthread
from kivy.utils import platform as kivy_platform
import speech_recognition as sr

import logging

import xai_sdk
from xai_sdk import Client
from xai_sdk.chat import user, system, assistant
import openai
from openai import OpenAI

# TTS libraries
if kivy_platform == "win":
    import edge_tts
    import asyncio
    import playsound
elif kivy_platform == "android":
    from jnius import autoclass
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    Locale = autoclass('java.util.Locale')
    tts_class = autoclass('android.speech.tts.TextToSpeech')

# TTS Workaround for Windows in case Edge TTS fails
def speak_windows_safe(message):
    safe_message = message.replace('"', '').replace('\n', ' ').replace('\r', ' ')
    os.system(f'mshta vbscript:Execute("CreateObject(""SAPI.SpVoice"").Speak(""{safe_message}"")(window.close)")')

# Queue for messages (for Android)
tts_queue = queue.Queue()

# VoiceAssistant App
class VoiceAssistant(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation='vertical', **kwargs)
        # Kivy UI
        self.orientation = 'vertical'
        # Set API Key
        apikey_layout = BoxLayout(orientation='horizontal', size_hint=(1, .06))
        self.apikey_input = TextInput(hint_text="Ingrese IA API Key", password=True, size_hint=(0.4, 1))
        apikey_layout.add_widget(self.apikey_input)
        self.apikey_button = Button(text="API Key", size_hint=(0.1, 1))
        self.apikey_button.bind(on_press=self.check_apikey)
        apikey_layout.add_widget(self.apikey_button)
        # Select XAI or OpenAI
        self.ai_spinner = Spinner(
            text='IA',
            values=('openai', 'xai'),
            size_hint=(0.1, 1)
        )
        self.ai_spinner.bind(text=self.on_ai_select)
        apikey_layout.add_widget(self.ai_spinner)
        # Select language
        self.language_spinner = Spinner(
            text='Idioma',
            values=('es-ES', 'en-US', 'fr-FR', 'pt-BR', 'de-DE', 'ja-JP', 'zh-CN'),
            size_hint=(0.1, 1)
        )
        self.language_spinner.bind(text=self.on_language_select)
        apikey_layout.add_widget(self.language_spinner)
        # Select voice style
        self.voice_spinner = Spinner(
            text='Selecciona voz',
            values=('es-PE-CamilaNeural', 'es-PE-AlexNeural',
                    'es-US-PalomaNeural', 'es-US-AlonsoNeural',
                    'es-AR-ElenaNeural', 'es-CO-SalomeNeural', 'es-ES-ElviraNeural',
                    'es-MX-DaliaNeural', 'es-VE-PaolaNeural',
                    'en-US-AvaMultilingualNeural', 'en-US-EmmaMultilingualNeural',
                    'en-US-AndrewMultilingualNeural', 'en-US-BrianMultilingualNeural'),
            size_hint=(0.3, 1)
        )
        self.voice_spinner.bind(text=self.on_voice_select)
        apikey_layout.add_widget(self.voice_spinner)
        self.add_widget(apikey_layout)
        # Show state (Label) & InputYOutput (TextInput)
        self.label = Label(text="Estados de la aplicación", size_hint=(1, .2), font_size='20sp')
        self.add_widget(self.label)
        self.text_input = TextInput(hint_text="Conversación...", multiline=True, size_hint=(1, .7))
        self.add_widget(self.text_input)
        # Start/Pause/Continue button
        self.pause_button = Button(text="Inicio", size_hint=(1, .05), font_size='20sp')
        self.pause_button.bind(on_press=self.toggle_pause)
        self.add_widget(self.pause_button)
        # Disable UI until API Key is set
        self.set_components_state(False)

        # TTS configuration depending on platform
        if kivy_platform == "win":
            # Edge TTS and Workaround don't need configuration
            pass
        elif kivy_platform == "android":
            self.tts = tts_class(PythonActivity.mActivity, None)
            self.tts.setLanguage(Locale("es", "ES"))
            self.tts_worker_thread = threading.Thread(target=self.tts_worker_android, daemon=True)
            self.tts_worker_thread.start()

        # Variables
        self.recognizer = sr.Recognizer() # Voice recognizer
        self.stop_while = False
        self.paused = True
        self.is_finishing = False
        self.end = False
        self.is_speaking = False
        self.voice = "es-PE-CamilaNeural"
        self.language = "es-ES"
        self.api_key = None
        self.ai = 'xai' # xai or openai
        self.xai_baseurl = "https://api.x.ai/"
        self.system_prompt = ("You are a personal assistant," +
            "for all your answers avoid special characters" +
            "and avoid long answers if possible."
        )
        self.openai_messages = [{"role": "developer", "content": self.system_prompt}] # Context for OpenAI

        # Thread for voice recognizer, AI response, TTS voice output
        self.listen_thread = threading.Thread(target=self.listen_loop, daemon=True)
        self.listen_thread.start()

    @mainthread
    # Update UI components
    def update_ui(self, user_text="", assistant_text="", label_text=None):
        if user_text:
            self.text_input.text += f"Usuario: {user_text}\n"
        if assistant_text:
            self.text_input.text += f"IA: {assistant_text}\n"
        if label_text is not None:
            self.label.text = label_text

    # Speak procedure for Android & Windows workaround
    def speak(self, message):
        def tts_worker():
            self.is_speaking = True
            if kivy_platform == "win":
                speak_windows_safe(message)
            elif kivy_platform == "android":
                self.tts.speak(message, tts_class.QUEUE_FLUSH, None, "")
            self.is_speaking = False
        threading.Thread(target=tts_worker, daemon=True).start()
    
    # Speak procedure for Windows Edge TTS
    def speak_edgetts(self, message):
        def tts_worker():
            self.is_speaking = True
            try:
                with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as temp_mp3:
                    mp3_path = temp_mp3.name
                asyncio.run(edge_tts.Communicate(message, voice= self.voice).save(mp3_path))
                playsound.playsound(mp3_path)
            except Exception as e:
                logging.info("Edge TTS error:", e)
            finally:
                try:
                    os.remove(mp3_path)
                except Exception as e:
                    print("TTS Remove audio file error:", e)
                self.is_speaking = False
                if self.is_finishing: self.end = True
        threading.Thread(target=tts_worker, daemon=True).start()
    
    # Speak procedure for Android
    def tts_worker_android(self):
        while True:
            message = tts_queue.get()
            self.tts.speak(message, tts_class.QUEUE_FLUSH, None, "")
            tts_queue.task_done()

    # Flow control, voice recognizer, STT, AI response, TTS procedure
    def listen_loop(self):
        while not self.stop_while:
            if self.end: App.get_running_app().stop()
            if self.paused:
                self.update_ui(label_text="En pausa")
                time.sleep(0.2)
                continue
            if self.is_speaking:
                self.update_ui(label_text="Respuesta ...")
                continue
            with sr.Microphone() as source:
                try:
                    self.update_ui(label_text="Escuchando ...")
                    #audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=7)
                    audio = self.recognizer.listen(source, timeout=3)
                    self.update_ui(label_text="Reconociendo voz ...")
                    query = self.recognizer.recognize_google(audio, language=self.language)
                    self.update_ui(user_text=query)
                    self.update_ui(label_text="Esperando respuesta ...")
                    respuesta = self.process_query(query)
                    self.update_ui(assistant_text=respuesta)
                    self.speak_edgetts(respuesta)
                except Exception as e:
                    self.update_ui(label_text=f"Reconociendo voz: Error {e}")
                    logging.info(f"Speech recognition: {e}")
                    time.sleep(0.25)

    # Pausa/Continuar
    def toggle_pause(self, instance):
        self.paused = not self.paused
        if self.paused:
            self.pause_button.text = "Continuar"
            self.ai_spinner.disabled = False
            self.apikey_button.disabled = False
        else:
            self.pause_button.text = "Pausa"
            self.ai_spinner.disabled = True
            self.apikey_button.disabled = True

    # AI API: Send input to AI and return output
    def process_query(self, query):
        query = query.lower()
        if query == "terminar":
            self.is_finishing = True
            return("Entonces termino esta sesión. Hasta luego!")
        else:
            if self.ai == 'xai':
                self.chat.append(user(query)) # input
                response = self.chat.sample()
                self.chat.append(assistant(response.content)) # Add output as context for new inputs
                return response.content
            elif self.ai == 'openai':
                self.openai_messages.append({"role": "user", "content": query}) # input
                response = self.client.chat.completions.create(
                    model = "gpt-4.1",
                    messages = self.openai_messages
                )
                self.openai_messages.append({
                    "role": "assistant",
                    "content": response.choices[0].message.content
                }) # Add output as context for new inputs
                return response.choices[0].message.content
    
    # Enable/Disable UI components
    def set_components_state(self, enabled):
        self.text_input.disabled = not enabled
        self.pause_button.disabled = not enabled    

    # Procedure for API Key button
    def check_apikey(self, instance):
        if not self.apikey_input.text == "":
            self.api_key = self.apikey_input.text
            # Open session with AI cloud
            if self.ai == 'xai':
                self.client = Client(api_key=self.api_key, timeout=3600,)
            elif self.ai == 'openai':
                os.environ['OPENAI_API_KEY'] = self.api_key
                self.client = OpenAI()
            # Check if api key is valid
            try:
                if self.ai == 'xai':
                    self.check_xai(self.client)
                elif self.ai == 'openai': 
                    self.check_openai(self.client)                
            except Exception as e:
                self.update_ui(assistant_text=f"API key error. {str(e)}")

    # Procedure for voice select control
    def on_voice_select(self, spinner, text):
        self.voice = text
        logging.info(f"Selected voice: {text}")
    
    # Procedure for language select control
    def on_language_select(self, spinner, text):
        self.language = text
        logging.info(f"Selected language: {text}")

    # Procedure for ai select control
    def on_ai_select(self, spinner, text):
        self.ai = text
        logging.info(f"Selected ai: {text}")
    
    # OpenAI api key validation
    def check_openai(self, client):
        response = client.responses.create(
            model="gpt-4.1",
            input="Hola!"
        )
        self.update_ui(assistant_text=f"OpenAI API key ok. Pulsa Inicio")
        # Enable/Disable UI components
        self.set_components_state(True)
        self.ai_spinner.disabled = True
        self.apikey_button.disabled = True

    # XAI api key validation
    def check_xai(self, client):
        url = self.xai_baseurl + 'v1/api-key'
        headers = {
            "Authorization": f"Bearer {self.api_key}"
        }
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            # Set XAI chat session
            self.chat = client.chat.create(model="grok-4")
            self.chat.append(system(self.system_prompt))
            self.update_ui(assistant_text=f"Xai API key {response.status_code} ok. Pulsa Inicio")
            # Enable/Disable UI components
            self.set_components_state(True)
            self.ai_spinner.disabled = True
            self.apikey_button.disabled = True
        else:
            self.update_ui(assistant_text=f"Xai API key error. {response.text} ")

# Main Class
class VoiceAssistantApp(App):
    def build(self):
        return VoiceAssistant()

if __name__ == "__main__":
    VoiceAssistantApp().run()