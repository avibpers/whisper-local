# platform/macos/gpu.py
# No-op GPU probe. CUDA/ROCm acceleration is Windows/Linux only; Apple Silicon
# users get their speedup from the whisper.cpp backend instead.
# Windows mirror: platform/windows/gpu.py (the real detection).
def detect_and_print(configured_device):
    return (None, None, False)


# No CUDA backend on macOS, so there is never anything missing.
def missing_gpu_libraries():
    return []


# The header monitor's readers: no NVIDIA card or Windows counters here.
def gpu_memory(index):
    return None


def process_gpu_memory_mb(pid):
    return None
