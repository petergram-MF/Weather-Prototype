import requests

url = "https://api.tomorrow.io/v4/weather/forecast?location=new%20york&apikey=ZM2vE4vcJObGcYh8PSRXWA9l2J7F0q5i"

headers = {
    "accept-encoding": "deflate, gzip, br",
    "accept": "application/json"
}

response = requests.get(url, headers=headers)

print(response.text)