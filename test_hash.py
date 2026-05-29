import hashlib, os

for line in open(".env"):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()

password = os.environ.get("GROWATT_PASSWORD")
md5 = hashlib.md5(password.encode()).hexdigest()
result = ""
for i in range(0, len(md5), 2):
    pair = md5[i:i+2]
    result += "c" + pair if pair in ("00","c8","c0","1d") else pair
print("MD5:", md5)
print("Transformed:", result)
