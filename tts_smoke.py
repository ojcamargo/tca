from gtts import gTTS
from pathlib import Path
import os
out = Path(os.path.expandvars(r"%USERPROFILE%\Downloads\tts_smoke.mp3"))
gTTS("teste de voz", lang="pt", tld="com.br").save(str(out))
print("OK:", out, out.exists(), out.stat().st_size if out.exists() else 0)
