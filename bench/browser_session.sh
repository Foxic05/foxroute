#!/bin/bash
# Браузер на сервере для входа в аккаунты руками.
#
# Зачем: куки веб-сессий добываются только настоящим входом, а вводить пароли
# должен сам человек. Поднимаем Chrome на виртуальном экране, отдаём картинку
# по VNC и ждём, пока в него войдут.
#
# БЕЗОПАСНОСТЬ: и VNC, и websockify слушают ТОЛЬКО 127.0.0.1. Наружу порты не
# открываются, доступ — через SSH-туннель. Иначе чужой браузер с живыми
# сессиями оказался бы доступен всему интернету.
#
# Chrome здесь идёт с --no-sandbox: под root он иначе отказывается
# запускаться вовсе. Песочница браузера тут и не защищает — сервер наш, а
# страницы открывает человек руками.
#
#   старт:  bash browser_session.sh start
#   стоп:   bash browser_session.sh stop
#   статус: bash browser_session.sh status

set -u
# Профиль можно подменить: FOXROUTE_PROFILE=/путь bash browser_session.sh start.
# Пригодилось, когда вход в Яндекс нашёлся в профиле старого проекта —
# заново войти туда уже не давали.
PROFILE=${FOXROUTE_PROFILE:-/opt/foxroute/data/browser_profile}
DISPLAY_NUM=:77
VNC_PORT=5977
WEB_PORT=6977
DEBUG_PORT=9977
LOG=/tmp/browser_session.log

start() {
    mkdir -p "$PROFILE"
    pkill -f "Xvfb $DISPLAY_NUM" 2>/dev/null
    pkill -f "x11vnc.*$VNC_PORT" 2>/dev/null
    pkill -f "websockify.*$WEB_PORT" 2>/dev/null
    pkill -f "chrome.*$PROFILE" 2>/dev/null
    sleep 1

    Xvfb "$DISPLAY_NUM" -screen 0 1440x900x24 >>"$LOG" 2>&1 &
    sleep 2

    # -localhost и -rfbport на петле: снаружи не достучаться.
    x11vnc -display "$DISPLAY_NUM" -rfbport "$VNC_PORT" -localhost \
           -forever -shared -nopw -quiet >>"$LOG" 2>&1 &
    sleep 1

    if [ -d /usr/share/novnc ]; then
        websockify --web=/usr/share/novnc 127.0.0.1:"$WEB_PORT" \
                   127.0.0.1:"$VNC_PORT" >>"$LOG" 2>&1 &
    else
        websockify 127.0.0.1:"$WEB_PORT" 127.0.0.1:"$VNC_PORT" >>"$LOG" 2>&1 &
    fi

    # Отладочный порт нужен, чтобы забрать доступы скриптом. Половина
    # сервисов держит их НЕ в куках, а в localStorage (Qwen, Z.ai,
    # DeepSeek), и из файлов профиля это достаётся через LevelDB — то есть
    # тяжело и ненадёжно. Через отладочный порт то и другое читается ровно
    # так, как их видит страница.
    #
    # Слушает только петлю, как VNC: наружу порт не открывается.
    DISPLAY="$DISPLAY_NUM" google-chrome \
        --user-data-dir="$PROFILE" \
        --no-first-run --no-default-browser-check --no-sandbox \
        --disable-gpu --disable-dev-shm-usage \
        --remote-debugging-address=127.0.0.1 \
        --remote-debugging-port="$DEBUG_PORT" \
        --window-size=1440,900 \
        --window-position=0,0 \
        "about:blank" >>"$LOG" 2>&1 &

    sleep 4
    echo "браузер поднят"
    echo "  профиль: $PROFILE"
    echo "  порты:   127.0.0.1:$VNC_PORT (VNC), 127.0.0.1:$WEB_PORT (noVNC),"
    echo "           127.0.0.1:$DEBUG_PORT (отладка) — наружу закрыты"
    echo
    echo "  1) на своей машине подними туннель:"
    echo "     ssh -N -L $WEB_PORT:127.0.0.1:$WEB_PORT root@SERVER_IP"
    echo "  2) открой в браузере: http://localhost:$WEB_PORT/vnc.html"
    echo "  3) после входа в аккаунты:  python3 bench/harvest_cookies.py"
}

stop() {
    pkill -f "chrome.*$PROFILE" 2>/dev/null
    pkill -f "websockify.*$WEB_PORT" 2>/dev/null
    pkill -f "x11vnc.*$VNC_PORT" 2>/dev/null
    pkill -f "Xvfb $DISPLAY_NUM" 2>/dev/null
    echo "остановлено (профиль сохранён: $PROFILE)"
}

status() {
    for what in "Xvfb $DISPLAY_NUM" "x11vnc.*$VNC_PORT" \
                "websockify.*$WEB_PORT" "chrome.*$PROFILE"; do
        if pgrep -f "$what" >/dev/null; then echo "  идёт:   $what"
        else echo "  стоит:  $what"; fi
    done
    echo "── слушают (должно быть только 127.0.0.1) ──"
    ss -ltnp 2>/dev/null | grep -E "$VNC_PORT|$WEB_PORT|$DEBUG_PORT" || echo "  портов нет"
}

case "${1:-status}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *) echo "bash browser_session.sh start|stop|status"; exit 2 ;;
esac
