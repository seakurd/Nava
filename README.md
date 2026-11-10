# Nava Kurdish TTS models
Meta MMS-TTS (VITS) exported to ONNX and int8-quantized for on-device / server synthesis.
- kmr: facebook/mms-tts-kmr-script_latin (CC-BY-NC-4.0)
- ckb: razhan/mms-tts-ckb (MMS fine-tune)
Input: character ids from vocab.json, interleaved with 0 (add_blank). Output: float waveform 16 kHz.
