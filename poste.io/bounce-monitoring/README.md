# Разовй запуск
```
docker cp ./poste.io/bounce-monitor/rescan.py poste-bounce-monitor:/app/rescan.py
```
### Запустить разовый прогон за сегодня
```
docker exec -it poste-bounce-monitor python /app/rescan.py
```
### Или за конкретную дату
```
docker exec -it poste-bounce-monitor python /app/rescan.py 2026-10-09
```

```
docker compose up -d --force-recreate poste-bounce-monitor

docker exec poste-bounce-monitor printenv LOCAL_DOMAINS


docker cp ./bounce-monitor/rescan.py poste-bounce-monitor:/app/rescan.py

```