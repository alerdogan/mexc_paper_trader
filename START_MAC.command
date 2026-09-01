#!/bin/zsh

cd "$(dirname "$0")"

echo "=========================================="
echo " MEXC Paper Trader - macOS Baslatici"
echo " Port: 8071 | Mod: PAPER"
echo "=========================================="
echo ""

fail() {
  echo ""
  echo "HATA: $1"
  echo ""
  echo "Bu pencereyi kapatmadan hata metnini bana gönder."
  echo "Cikmak icin Enter'a bas."
  read
  exit 1
}

# Tercih sirasi: 3.13 -> 3.12 -> 3.11 -> 3.10.
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3.10; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  fail "Python 3.10 veya daha yeni bir surum bulunamadi. Python 3.13 onerilir."
fi

PY_VERSION="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')" || fail "Python surumu okunamadi."
PY_MM="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

echo "Secilen Python: $PY_VERSION"
echo "Yol: $PYTHON_BIN"

# Mevcut venv farkli Python ana surumuyle olusturulduysa otomatik yenile.
REBUILD_VENV=0
if [ -d ".venv" ]; then
  if [ ! -x ".venv/bin/python" ]; then
    REBUILD_VENV=1
  else
    VENV_MM="$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)"
    if [ "$VENV_MM" != "$PY_MM" ]; then
      echo ""
      echo "Eski sanal ortam Python $VENV_MM ile olusturulmus. Python $PY_MM icin yenileniyor..."
      REBUILD_VENV=1
    fi
  fi
fi

if [ "$REBUILD_VENV" -eq 1 ]; then
  rm -rf .venv || fail "Eski .venv silinemedi."
fi

if [ ! -d ".venv" ]; then
  echo ""
  echo "Sanal Python ortami olusturuluyor..."
  "$PYTHON_BIN" -m venv .venv || fail "Python sanal ortami (.venv) olusturulamadi."
fi

source .venv/bin/activate || fail "Sanal ortam aktif edilemedi."

echo "Aktif ortam: $(python --version 2>&1)"
echo ""
echo "Gerekli kutuphaneler kontrol ediliyor..."
python -m pip install --disable-pip-version-check -r requirements.txt || fail "Python kutuphaneleri yuklenemedi."

echo ""
echo "Uygulama baslatiliyor..."
python app.py &
APP_PID=$!

cleanup() {
  if kill -0 $APP_PID >/dev/null 2>&1; then
    kill $APP_PID >/dev/null 2>&1
  fi
}
trap cleanup EXIT INT TERM

for i in {1..30}; do
  if curl -s http://127.0.0.1:8071/api/status >/dev/null 2>&1; then
    echo ""
    echo "HAZIR: http://127.0.0.1:8071"
    open "http://127.0.0.1:8071"
    wait $APP_PID
    exit $?
  fi

  if ! kill -0 $APP_PID >/dev/null 2>&1; then
    fail "Uygulama baslarken kapandi. Yukaridaki Python hata metnini bana gönder."
  fi
  sleep 1
done

kill $APP_PID >/dev/null 2>&1
fail "8071 portundaki uygulama 30 saniye icinde baslamadi."
