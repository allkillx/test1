"""One-off discovery: where NOS / network data and map geometry live."""
import io, json, os, re, zipfile, csv
import requests

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (nem-live-discovery)"
BASE = "https://nemweb.com.au"


def hrefs(path):
    r = S.get(BASE + path, timeout=60)
    r.raise_for_status()
    return re.findall(r'HREF="([^"]+)"', r.text, flags=re.I)


def tables_of(href, nrows=2):
    r = S.get(BASE + href, timeout=120)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    for name in z.namelist()[:1]:
        text = z.read(name).decode("utf-8", "replace")
        heads, counts, samples = {}, {}, {}
        for p in csv.reader(io.StringIO(text)):
            if not p:
                continue
            if p[0] == "I":
                heads[(p[1], p[2])] = p[4:]
            elif p[0] == "D":
                k = (p[1], p[2]); counts[k] = counts.get(k, 0) + 1
                if counts[k] <= nrows:
                    samples.setdefault(k, []).append(p[4:])
        for k, cols in heads.items():
            print(f"    TABLE {k[0]}.{k[1]} rows={counts.get(k,0)}")
            print(f"      cols={cols}")
            for s in samples.get(k, []):
                print(f"      e.g. {dict(zip(cols, s))}")


print("== /Reports/Current/ ==")
cur = hrefs("/Reports/Current/")
print([h.rstrip('/').split('/')[-1] for h in cur])
for h in cur:
    name = h.rstrip("/").split("/")[-1]
    if re.search(r"network|outage|nos|constraint|gencon|mtpasa_dupd|ancillary", name, re.I):
        try:
            fs = hrefs(h if h.startswith("/") else "/Reports/Current/" + h)
            fs = [f for f in fs if not f.endswith("/")]
            print(f"\n-- {name}: {len(fs)} files, last 3: {fs[-3:]}")
            z = [f for f in fs if f.lower().endswith(".zip")]
            if z:
                tables_of(z[-1])
        except Exception as e:
            print(f"-- {name}: error {e}")

print("\n== GA services ==")
for url in ["https://services.ga.gov.au/gis/rest/services?f=json",
            "https://services.ga.gov.au/gis/rest/services/Foundation_Electricity_Infrastructure/MapServer?f=json",
            "https://services.ga.gov.au/gis/rest/services/National_Electricity_Infrastructure/MapServer?f=json"]:
    try:
        r = S.get(url, timeout=60)
        j = r.json()
        if "services" in j:
            print(url, [s["name"] for s in j["services"] if re.search(r"elec|energy|infra|boundar|state", s["name"], re.I)])
            print("  folders:", j.get("folders"))
        else:
            print(url, "layers:", [(l["id"], l["name"]) for l in j.get("layers", [])])
    except Exception as e:
        print(url, "error", e)

print("\n== OpenElectricity ==")
key = os.environ.get("OPENELECTRICITY_API_KEY")
print("key present:", bool(key))
if key:
    try:
        r = S.get("https://api.openelectricity.org.au/v4/facilities/", headers={"Authorization": f"Bearer {key}"},
                  params={"network_id": "NEM"}, timeout=60)
        j = r.json()
        print("status", r.status_code, "keys", list(j)[:10])
        d = j.get("data") or []
        print("n facilities", len(d))
        print(json.dumps(d[:1], indent=1)[:1500])
    except Exception as e:
        print("error", e)
