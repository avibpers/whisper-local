import warnings

from . import instance_lock, keyboard, hotkeys, paths, permissions

# pycaw reads every property of every audio device and warns for each one it
# can't read; over Remote Desktop one does that on every meeting check,
# flooding the console. Koli never uses those properties.
warnings.filterwarnings('ignore', message='COMError attempting to get property', category=UserWarning,
                        module='pycaw')
