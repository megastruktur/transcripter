"""DiariZen pilot: diarize a WAV, write RTTM + stats to stdout."""
import sys
import time
from pathlib import Path

from diarizen.pipelines.inference import DiariZenPipeline

wav = Path(sys.argv[1])
out_rttm = Path(sys.argv[2])
out_rttm.parent.mkdir(parents=True, exist_ok=True)

import os
model_id = os.environ.get("DIARIZEN_MODEL", "BUT-FIT/diarizen-wavlm-base-s80-md")
print(f"[pilot] model: {model_id}", flush=True)

t0 = time.time()
pipeline = DiariZenPipeline.from_pretrained(model_id, rttm_out_dir=str(out_rttm.parent))
t_load = time.time() - t0
print(f"[pilot] model load: {t_load:.1f}s", flush=True)

t0 = time.time()
result = pipeline(str(wav), sess_name=out_rttm.stem)
t_infer = time.time() - t0

import torchaudio
info = torchaudio.info(str(wav))
dur = info.num_frames / info.sample_rate

speakers = sorted({label for _, _, label in result.itertracks(yield_label=True)})
print(f"[pilot] inference: {t_infer:.1f}s for {dur:.1f}s audio "
      f"(RTF {t_infer / dur:.3f})", flush=True)
print(f"[pilot] speakers: {len(speakers)} {speakers}", flush=True)
