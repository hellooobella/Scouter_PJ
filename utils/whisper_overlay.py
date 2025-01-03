import os
import cv2
import time
import whisper
import asyncio
import tempfile
import threading
import numpy as np
import sounddevice as sd
import scipy.io.wavfile as wav

from queue import Queue
from utils.asyncllm import LLMClient
from PIL import Image, ImageDraw, ImageFont

class LLMOverlay:
    def __init__(self, llm_provider="google", api_key=None, model_name=None):
        # STT 모델 초기화
        self.model = whisper.load_model("tiny")
        self.sample_rate = 16000
        
        # LLM 초기화
        self.llm_client = LLMClient(api_key=api_key, provider=llm_provider)
        self.model_name = model_name
        
        # 상태 관리
        self.is_recording = False
        self.is_processing = False
        self.current_text = None
        self.text_timestamp = None
        self.text_display_duration = 15  # 15초 표시
        
        # 처리 큐 및 스레드 설정
        self.processing_queue = Queue()
        self.should_run = True
        self.process_thread = threading.Thread(target=self._process_queue)
        self.process_thread.daemon = True
        self.process_thread.start()

    def start_recording(self, duration=5):
        if not self.is_recording:
            self.is_recording = True
            self.recording_thread = threading.Thread(
                target=self._record_audio,
                args=(duration,)
            )
            self.recording_thread.daemon = True
            self.recording_thread.start()
            return True
        return False

    def stop_recording(self):
        if self.is_recording:
            self.is_recording = False
            return True
        return False

    def _record_audio(self, duration):
        try:
            audio_data = sd.rec(
                int(duration * self.sample_rate),
                samplerate=self.sample_rate,
                channels=1,
                dtype='float32'
            )
            sd.wait()
            self.is_recording = False
            self.is_processing = True
            self._transcribe_audio(audio_data)
        except Exception as e:
            print(f"LLM Recording error: {e}")
        finally:
            self.is_recording = False

    def _transcribe_audio(self, audio_data):
        try:
            # Save audio to temporary file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp_audio_file:
                wav.write(temp_audio_file.name, self.sample_rate, 
                         (audio_data * np.iinfo(np.int16).max).astype(np.int16))
                filename = temp_audio_file.name

            # Transcribe audio
            result = self.model.transcribe(
                filename,
                language="en",
                task="transcribe",
                fp16=False
            )
            
            transcribed_text = result["text"].strip()
            self.processing_queue.put(transcribed_text)
            
            os.remove(filename)
            
        except Exception as e:
            print(f"LLM Transcription error: {e}")
            self.is_processing = False

    async def process_with_llm(self, text):
        try:
            prompt = f"Your response will show in small screen. Shorten your response to 3 or 4 sentences about this user prompt:[{text}]"
            response = await self.llm_client.call_model(prompt=prompt, model=self.model_name)
            return response
        except Exception as e:
            print(f"LLM processing error: {e}")
            return f"Error processing request: {str(e)}"

    def _process_queue(self):
        while self.should_run:
            try:
                if not self.processing_queue.empty():
                    text = self.processing_queue.get()
                    # Create event loop for async operation
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    response = loop.run_until_complete(self.process_with_llm(text))
                    loop.close()
                    
                    self.current_text = response
                    self.text_timestamp = time.time()
                    self.is_processing = False
                time.sleep(0.1)
            except Exception as e:
                print(f"Queue processing error: {e}")
                self.is_processing = False

    def draw_text(self, frame):
        if frame is None or not (self.current_text or self.is_processing):
            return

        if self.current_text and self.text_timestamp:
            if time.time() - self.text_timestamp > self.text_display_duration:
                self.current_text = None
                self.text_timestamp = None
                return

        display_text = "Processing LLM response..." if self.is_processing else self.current_text

        if not display_text:
            return

        frame_height, frame_width = frame.shape[:2]
        max_width = int(frame_width * 5/6)  # 화면 너비의 5/6
        text_y = 50  # 상단 여백

        try:
            img_pil = Image.fromarray(frame)
            draw = ImageDraw.Draw(img_pil)

            # 폰트 크기를 2/3로 줄임 (기존 30에서 20으로)
            try:
                font = ImageFont.truetype("malgun.ttf", 20)
            except:
                try:
                    font = ImageFont.truetype("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", 20)
                except:
                    try:
                        font = ImageFont.truetype("/usr/share/fonts/nanum/NanumGothic.ttf", 20)
                    except:
                        font = ImageFont.load_default()

            # 텍스트를 여러 줄로 나누기
            words = display_text.split()
            lines = []
            current_line = []
            current_width = 0

            for word in words:
                word_width = draw.textlength(word + " ", font=font)
                if current_width + word_width <= max_width:
                    current_line.append(word)
                    current_width += word_width
                else:
                    lines.append(" ".join(current_line))
                    current_line = [word]
                    current_width = word_width

            if current_line:
                lines.append(" ".join(current_line))

            # 모든 텍스트의 높이 계산
            line_height = int(font.size * 1.5)  # 줄 간격
            total_text_height = len(lines) * line_height

            # 배경 크기 계산
            padding_x = 20
            padding_y = 10
            bg_y1 = text_y - padding_y
            bg_y2 = bg_y1 + total_text_height + 2 * padding_y
            bg_width = max_width + 2 * padding_x
            bg_x1 = (frame_width - bg_width) // 2
            bg_x2 = bg_x1 + bg_width

            # 반투명 배경 그리기
            overlay = frame.copy()
            cv2.rectangle(overlay,
                        (int(bg_x1), int(bg_y1)),
                        (int(bg_x2), int(bg_y2)),
                        (0, 0, 0), -1)
            cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)

            # 각 줄의 텍스트를 중앙 정렬하여 그리기
            img_pil = Image.fromarray(frame)
            draw = ImageDraw.Draw(img_pil)
            
            current_y = text_y
            for line in lines:
                line_width = draw.textlength(line, font=font)
                text_x = (frame_width - line_width) // 2  # 각 줄마다 중앙 정렬
                draw.text(
                    (text_x, current_y),
                    line,
                    font=font,
                    fill=(255, 255, 255)
                )
                current_y += line_height

            frame[:] = np.array(img_pil)

        except Exception as e:
            print(f"Draw LLM text error: {e}")

    def cleanup(self):
        self.should_run = False
        if self.process_thread.is_alive():
            self.process_thread.join(timeout=1)

class WhisperSTTOverlay:
    # Initialize the STT overlay system
    # STT 오버레이 시스템 초기화
    def __init__(self, model_type="medium"):
        self.model = whisper.load_model(model_type)
        # Standard sample rate for Whisper
        # Whisper의 표준 샘플링 레이트
        self.sample_rate = 16000
        self.is_processing = False
        self.is_recording = False
        self.current_text = None
        self.text_timestamp = None
        self.text_display_duration = 10
        self.processing_queue = Queue()
        self.should_run = True
        # Start processing thread
        # 처리 스레드 시작
        self.process_thread = threading.Thread(target=self._process_queue)
        self.process_thread.daemon = True
        self.process_thread.start()

    # Process audio queue in background thread
    # 백그라운드 스레드에서 오디오 큐 처리
    def _process_queue(self):
        while self.should_run:
            try:
                if not self.processing_queue.empty():
                    audio_data = self.processing_queue.get()
                    self._transcribe_audio(audio_data)
                time.sleep(0.1)
            except Exception as e:
                print(f"Queue processing error: {e}")

    # Start background processing thread
    # 백그라운드 처리 스레드 시작
    def start_background_processing(self):
        self.processing_thread = threading.Thread(target=self._process_audio_queue)
        self.processing_thread.daemon = True
        self.processing_thread.start()
    
    # Process audio queue continuously
    # 오디오 큐 연속 처리
    def _process_audio_queue(self):
        while True:
            try:
                if not self.processing_queue.empty():
                    audio_data = self.processing_queue.get()
                    self._transcribe_audio(audio_data)
                time.sleep(0.1)
            except Exception as e:
                print(f"Error in process_audio_queue: {e}")
                continue
    
    # Start audio recording for specified duration
    # 지정된 시간 동안 오디오 녹음 시작
    def start_recording(self, duration=5):
        if not self.is_recording:
            self.is_recording = True
            self.recording_thread = threading.Thread(
                target=self._record_audio,
                args=(duration,)
            )
            self.recording_thread.daemon = True
            self.recording_thread.start()
            return True
        return False

    # Stop ongoing recording
    # 진행 중인 녹음 중지
    def stop_recording(self):
        if self.is_recording:
            self.is_recording = False
            return True
        return False

    # Record audio using sounddevice
    # sounddevice를 사용하여 오디오 녹음
    def _record_audio(self, duration):
        try:
            audio_data = sd.rec(
                int(duration * self.sample_rate),
                samplerate=self.sample_rate,
                channels=1,
                dtype='float32'
            )
            sd.wait()
            self.is_recording = False
            self.is_processing = True
            self.processing_queue.put(audio_data)
        except Exception as e:
            print(f"Recording error: {e}")
        finally:
            self.is_recording = False
            self.is_processing = False

    # Transcribe audio using Whisper model
    # Whisper 모델을 사용하여 오디오 텍스트 변환
    def _transcribe_audio(self, audio_data):
        try:
            # Save audio to temporary file
            # 임시 파일로 오디오 저장
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp_audio_file:
                wav.write(temp_audio_file.name, self.sample_rate, 
                         (audio_data * np.iinfo(np.int16).max).astype(np.int16))
                filename = temp_audio_file.name

            # Perform transcription
            # 음성 인식 수행
            result = self.model.transcribe(
                filename,
                language="en",
                task="transcribe",
                fp16=False
            )
            
            self.current_text = result["text"].strip()
            self.text_timestamp = time.time()
            
            os.remove(filename)
            
        except Exception as e:
            print(f"Transcription error: {e}")
            self.current_text = "음성 인식 실패"
            self.text_timestamp = time.time()
        finally:
            self.is_processing = False

    # Calculate text position considering overlay bounds
    # 오버레이 경계를 고려한 텍스트 위치 계산
    def get_text_position(self, frame_width, frame_height, overlay_bbox=None):
        text_y = frame_height - 50
        text_x = frame_width // 2
        
        if overlay_bbox:
            x, y, w, h = overlay_bbox
            overlay_center = x + w//2
            
            # Adjust text position based on overlay position
            # 오버레이 위치에 따라 텍스트 위치 조정
            if x < frame_width // 2:
                text_x = int(frame_width * 0.75)
            else:
                text_x = int(frame_width * 0.25)
        
        return text_x, text_y

    # Draw text overlay on frame
    # 프레임에 텍스트 오버레이 그리기
    def draw_text(self, frame):
        try:
            if frame is None:
                return

            # Check if text should be cleared
            # 텍스트 제거 여부 확인
            if self.current_text and self.text_timestamp and time.time() - self.text_timestamp > self.text_display_duration:
                self.current_text = None
                self.text_timestamp = None
                return

            # Determine display text based on current state
            # 현재 상태에 따른 표시 텍스트 결정
            if self.is_recording:
                display_text = "Recording..."
            elif self.is_processing:
                display_text = "Processing..."
            elif self.current_text:
                display_text = self.current_text
            else:
                return

            try:
                # Draw text using PIL (preferred method for Korean text)
                # PIL을 사용하여 텍스트 그리기 (한글 텍스트를 위한 선호 방법)
                frame_height, frame_width = frame.shape[:2]
                text_x = frame_width // 2
                text_y = frame_height - 50

                img_pil = Image.fromarray(frame)
                draw = ImageDraw.Draw(img_pil)

                # Try loading Korean fonts in different locations
                # 다양한 위치에서 한글 폰트 로드 시도
                try:
                    font = ImageFont.truetype("malgun.ttf", 30)
                except:
                    try:
                        font = ImageFont.truetype("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", 30)
                    except:
                        try:
                            font = ImageFont.truetype("/usr/share/fonts/nanum/NanumGothic.ttf", 30)
                        except:
                            print("Warning: Using default font as Korean fonts not found")
                            font = ImageFont.load_default()

                # Calculate text dimensions and background
                # 텍스트 크기와 배경 계산
                bbox = draw.textbbox((0, 0), display_text, font=font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]

                # Draw semi-transparent background
                # 반투명 배경 그리기
                bg_x1 = text_x - text_width//2 - 10
                bg_x2 = text_x + text_width//2 + 10
                bg_y1 = text_y - text_height - 10
                bg_y2 = text_y + 10

                overlay = frame.copy()
                cv2.rectangle(overlay,
                            (int(bg_x1), int(bg_y1)),
                            (int(bg_x2), int(bg_y2)),
                            (0, 0, 0), -1)
                cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)

                # Draw text
                # 텍스트 그리기
                img_pil = Image.fromarray(frame)
                draw = ImageDraw.Draw(img_pil)
                draw.text(
                    (bg_x1 + 10, bg_y1 + 5),
                    display_text,
                    font=font,
                    fill=(255, 255, 255)
                )

                frame[:] = np.array(img_pil)

            except ImportError as e:
                # Fallback to OpenCV text rendering if PIL fails
                # PIL 실패 시 OpenCV 텍스트 렌더링으로 대체
                print(f"PIL import error: {e}. Falling back to OpenCV text rendering")
                font_scale = 0.8
                thickness = 2
                text_size = cv2.getTextSize(display_text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)[0]
                text_w, text_h = text_size

                bg_x1 = text_x - text_w//2 - 10
                bg_x2 = text_x + text_w//2 + 10
                bg_y1 = text_y - text_h - 10
                bg_y2 = text_y + 10

                overlay = frame.copy()
                cv2.rectangle(overlay, (bg_x1, bg_y1), (bg_x2, bg_y2), (0, 0, 0), -1)
                cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)

                cv2.putText(frame, display_text,
                        (text_x - text_w//2, text_y),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        font_scale, (255, 255, 255), thickness)

        except Exception as e:
            print(f"Draw text error: {e}")
    
    # Clean up resources
    # 리소스 정리
    def cleanup(self):
        self.should_run = False
        if self.process_thread.is_alive():
            self.process_thread.join(timeout=1)