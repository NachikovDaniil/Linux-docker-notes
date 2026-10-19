Дополнительный docker контейнер который необходимо дописать в docker compose с poste.io

```
  poste-bounce-monitor:
    build:
      context: ./bounce-monitor
    container_name: poste-bounce-monitor
    restart: unless-stopped

    environment:
      TZ: America/Chicago # указываем время как на почтовом сервере

      # Telegram
      TELEGRAM_BOT_TOKEN: "${TELEGRAM_BOT_TOKEN}"
      TELEGRAM_CHAT_ID: "${TELEGRAM_CHAT_ID}"

      # Как часто проверять логи
      CHECK_INTERVAL: "5"

      # Локальные домены вашей почты.
      # Нужны для определения исходящих сообщений.
      LOCAL_DOMAINS: "${LOCAL_DOMAINS}" # указываем домен как на почтовом сервере

    volumes:
      # ВАЖНО: только чтение
      - ./data/mail/log:/poste-logs:ro

      # Состояние монитора
      - ./bounce-monitor/state:/state

      - /etc/localtime:/etc/localtime:ro

    networks:
      - poste_network

```