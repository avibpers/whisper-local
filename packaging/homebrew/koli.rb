# Homebrew formula TEMPLATE for Koli (macOS / Linux via a personal tap).
#
# Koli has a large, ML-heavy dependency tree (ctranslate2, faster-whisper,
# sherpa-onnx, …) that isn't practical to vendor as Homebrew "resource" stanzas,
# so this formula installs the published PyPI package into an isolated virtualenv.
# That keeps it off the user's system Python while staying maintainable.
#
# This is NOT homebrew-core material (core forbids pip-from-network installs);
# it's meant for a personal tap: `brew tap drajb/tap && brew install koli`.
# See docs/distribution.md for how to set up the tap.
#
# Replace after a release exists:
#   <VERSION>      e.g. 0.10.0
#   <SDIST_SHA256> sha256 of the source tarball from PyPI (or the GitHub sdist)

class Koli < Formula
  include Language::Python::Virtualenv

  desc "Free, offline AI dictation — press a hotkey, speak, get text at the cursor"
  homepage "https://github.com/drajb/whisper-local"
  url "https://files.pythonhosted.org/packages/source/w/whisper-local/whisper_local-<VERSION>.tar.gz"
  sha256 "<SDIST_SHA256>"
  license "MIT"

  depends_on "python@3.12"

  # Installs koli and pulls its dependencies into the venv at install
  # time. (Resource pinning is intentionally omitted — see the header note.)
  def install
    virtualenv_create(libexec, "python3.12")
    system libexec/"bin/pip", "install", "koli==#{version}"
    bin.install_symlink libexec/"bin/koli"
    bin.install_symlink libexec/"bin/wl"
  end

  test do
    assert_match "koli", shell_output("#{bin}/koli --version")
  end
end
