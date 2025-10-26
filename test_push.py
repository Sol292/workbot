import requests, json
url = "https://workbot-worker-production.up.railway.app/api/push-job"
headers = {"Authorization": "Bearer 292", "Content-Type": "application/json"}
payload = {
  "job": {"id": 999001, "category": "Клининг", "address": "Тверь, тестовая 1", "when": "сегодня 18:00", "pay": "1000 ₽", "user_id": 1},
  "customer_contact": "@test",
  "callback_url": "https://example.com"
}
print(requests.post(url, headers=headers, data=json.dumps(payload)).text)
