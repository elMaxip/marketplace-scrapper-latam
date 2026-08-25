import pathlib, sys, json

def patch(path, old, new):
    p = pathlib.Path(path)
    raw = p.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n")
    if old not in text:
        print("MISS", path); return False
    text = text.replace(old, new, 1)
    p.write_bytes(text.replace("\n", nl).encode("utf-8"))
    print("ok", path, repr(nl))
    return True

for path, old, new in json.load(open(sys.argv[1], encoding="utf-8")):
    patch(path, old, new)
