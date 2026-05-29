import urllib.request, urllib.parse, json, http.cookiejar, os

for line in open('.env'):
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, _, v = line.partition('=')
        os.environ[k.strip()] = v.strip()

password = os.environ.get('GROWATT_PASSWORD')
print(f"Password length: {len(password)} chars")
print(f"First char: '{password[0]}'")
print(f"Last char: '{password[-1]}'")
print(f"Any spaces: {' ' in password}")
