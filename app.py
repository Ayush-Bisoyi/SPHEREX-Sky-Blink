"""SPHEREx Sky Blink backend.  Run: python -m uvicorn app:app --port 8000
Public IRSA/Vizier services: no API key needed.

/api/frames   position -> IRSA SIA -> cutouts -> aligned raw arrays (cached) -> rendered PNG frames + spectrum
/api/catalog  WISE (IRSA) / Gaia (Vizier) sources in the field, for overlays
"""
import base64, hashlib, io, json, os, pickle, time, traceback, warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.time import Time
from astropy.utils.data import conf
from astropy.wcs import WCS
from astroquery.ipac.irsa import Irsa
import pyvo
from astroquery.vizier import Vizier
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from scipy.ndimage import map_coordinates

warnings.filterwarnings("ignore")
conf.remote_timeout = 120
app = FastAPI(title="SPHEREx Sky Blink")
HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"                      # saved copies, so the demo survives an IRSA outage
CACHE_DIR.mkdir(exist_ok=True)
OFFLINE = os.environ.get("SKYBLINK_OFFLINE") == "1"   # use saved copies first, never wait on the network


def _p(prefix, key, ext):
    return CACHE_DIR / f"{prefix}_{hashlib.md5(repr(key).encode()).hexdigest()[:16]}.{ext}"


@app.exception_handler(Exception)
async def explain_errors(request, exc):
    traceback.print_exception(type(exc), exc, exc.__traceback__)   # full traceback stays in this terminal
    return JSONResponse({"detail": f"Server error: {type(exc).__name__}: {str(exc)[:200]}"}, status_code=500)

RAW = {}                       # aligned raw cutouts, so view/binning changes are instant
PIX = 6.15 / 3600
COLLECTIONS = ["spherex_dr1", "spherex_qr3"]
EPOCH_YEARS = 26.0


def parse_position(ra, dec, name, pmra, pmdec):
    if name:
        nf = CACHE_DIR / "names.json"
        names = json.loads(nf.read_text()) if nf.exists() else {}
        k = name.strip().lower()
        if k in names:
            c = SkyCoord(names[k][0] * u.deg, names[k][1] * u.deg)
        else:
            try:
                c = SkyCoord.from_name(name)
            except Exception:
                raise HTTPException(400, f"Could not resolve '{name}'. Enter RA and Dec instead.")
            names[k] = [float(c.ra.deg), float(c.dec.deg)]
            nf.write_text(json.dumps(names))
    else:
        try:
            c = SkyCoord(float(ra) * u.deg, float(dec) * u.deg)
        except (TypeError, ValueError):
            try:
                c = SkyCoord(ra, dec, unit=(u.hourangle, u.deg))
            except Exception:
                raise HTTPException(400, "Could not read RA/Dec. Use degrees or e.g. 08h55m10.8s / -07d14m42s.")
    ra_d, dec_d = c.ra.deg, c.dec.deg
    if pmra or pmdec:
        dec_d += pmdec / 3.6e6 * EPOCH_YEARS
        ra_d += pmra / 3.6e6 * EPOCH_YEARS / np.cos(np.radians(dec_d))
    return float(ra_d), float(dec_d)


def to_png(img, diff=False):
    if diff:                   # signed difference: grey = no change
        sc = np.nanpercentile(np.abs(img), 99)
        s = 0.5 + 0.5 * np.arcsinh(img / (sc if sc > 0 else 1) * 5) / np.arcsinh(5)
    else:
        img = img - np.nanmedian(img)
        lo, hi = np.nanpercentile(img, [2, 99.7])
        if not np.isfinite(lo) or hi <= lo:
            hi = lo + 1
        s = np.arcsinh(np.clip((img - lo) / (hi - lo), 0, None) * 10) / np.arcsinh(10)
    arr = (np.clip(np.nan_to_num(s), 0, 1) * 255).astype("uint8")[::-1]   # north up
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def fetch(job):
    url, mjd, det, tw, shape, ra, dec = job
    for attempt in range(3):
        try:
            with fits.open(url, memmap=False) as h:
                data = h["IMAGE"].data.astype("float32")
                hdr = h["IMAGE"].header
                wcs = WCS(hdr)
                try:
                    flagged = float(np.mean(h["FLAGS"].data != 0)) * 100
                except Exception:
                    flagged = None
                try:           # wavelength at the target pixel from the spectral WCS
                    px, py = wcs.world_to_pixel(SkyCoord(ra * u.deg, dec * u.deg))
                    sw = WCS(hdr, h, key="W", fix=False)
                    wave = float(sw.pixel_to_world(px, py)[0].to(u.um).value)
                except Exception:
                    wave = None
            break
        except Exception:
            time.sleep(2 * (attempt + 1))
    else:
        return None
    yy, xx = np.indices(shape)
    ix, iy = wcs.world_to_pixel(tw.pixel_to_world(xx, yy))
    out = map_coordinates(data, [iy, ix], order=1, mode="constant", cval=np.nan)
    if not np.isfinite(out).any():
        return None
    c = shape[0] // 2
    ring = (np.hypot(xx - c, yy - c) > 5) & (np.hypot(xx - c, yy - c) < max(8, min(12, c)))
    bg = np.nanmedian(out[ring]) if np.isfinite(out[ring]).any() else np.nanmedian(out)
    flux = float(np.nanmean(out[c - 1:c + 2, c - 1:c + 2]) - bg)   # MJy/sr above local background
    return {"arr": out, "mjd": mjd, "t": Time(mjd, format="mjd").iso[:16],
            "det": int(det), "wave": wave, "flux": flux, "flagged": flagged}


def sia_query(pos, tries=4):
    """IRSA's SIA service sometimes answers 502/503 for a moment; retry with backoff before giving up."""
    err = None
    for i in range(tries):
        try:
            return Irsa.query_sia(pos=pos, collection=COLLECTIONS)
        except Exception as e:
            err = e
            traceback.print_exception(type(e), e, e.__traceback__, limit=1)
            time.sleep(3 * (i + 1))
    raise HTTPException(503, f"IRSA's search service did not respond ({type(err).__name__}). "
                             "This is usually temporary on IRSA's side; try again in a minute.")


def fetch_live(ra_d, dec_d, size, det, n, tries=4):
    tbl = sia_query((SkyCoord(ra_d * u.deg, dec_d * u.deg), 1 * u.arcsec), tries)
    names = np.array([str(b) for b in tbl["energy_bandpassname"]])
    cap = n if det else max(5, n // 3)
    rows, total = [], 0
    for d in ([det] if det else range(1, 7)):
        sub = tbl[names == f"SPHEREx-D{d}"]
        total += len(sub)
        if not len(sub):
            continue
        sub.sort("t_min")
        if len(sub) > cap:
            sub = sub[np.unique(np.linspace(0, len(sub) - 1, cap).astype(int))]
        rows += [(r, d) for r in sub]
    if not rows:
        raise HTTPException(404, "No SPHEREx images cover this position (try another detector or position).")
    nx = int(round(size / PIX))
    tw = WCS(naxis=2)
    tw.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    tw.wcs.crval = [ra_d, dec_d]
    tw.wcs.crpix = [(nx + 1) / 2, (nx + 1) / 2]
    tw.wcs.cdelt = [-PIX, PIX]
    jobs = [(f"{r['access_url']}?center={ra_d},{dec_d}d&size={size}", float(r["t_min"]), d, tw, (nx, nx), ra_d, dec_d)
            for r, d in rows]
    with ThreadPoolExecutor(8) as ex:
        raw = [f for f in ex.map(fetch, jobs) if f]
    if not raw:
        raise HTTPException(502, "IRSA returned no usable cutouts. Try again in a moment.")
    return raw, total


def get_raw(ra_d, dec_d, size, det, n):
    key = (round(ra_d, 5), round(dec_d, 5), size, det, n)
    if key in RAW:
        return RAW[key]
    f = _p("raw", key, "pkl")
    if OFFLINE and f.exists():
        raw, total = pickle.loads(f.read_bytes())
        RAW[key] = (raw, total, "saved")
        return RAW[key]
    try:
        raw, total = fetch_live(ra_d, dec_d, size, det, n, tries=1 if f.exists() else 4)
    except Exception:
        if f.exists():         # IRSA unreachable: show the saved copy (not memorised, so live is retried next time)
            raw, total = pickle.loads(f.read_bytes())
            return raw, total, "saved"
        raise
    f.write_bytes(pickle.dumps((raw, total)))
    RAW[key] = (raw, total, "live")
    return RAW[key]


def avg(xs):
    xs = [x for x in xs if x is not None and np.isfinite(x)]
    return round(float(np.mean(xs)), 2) if xs else None


def rms(a):
    v = 1.4826 * np.nanmedian(np.abs(a - np.nanmedian(a)))
    return round(float(v), 3) if np.isfinite(v) else None


def render(raw, view, k):
    out = []
    for d in sorted({r["det"] for r in raw}):
        g = sorted([r for r in raw if r["det"] == d], key=lambda r: r["mjd"])
        arrs = np.array([r["arr"] for r in g])
        if view == "bands":    # add everything from one detector array -> higher S/N
            m = np.nanmean(arrs, 0)
            out.append({"img": to_png(m), "t": f"{len(g)} exposures stacked", "det": d, "rms": rms(m),
                        "wave": avg([r["wave"] for r in g]), "flux": avg([r["flux"] for r in g]),
                        "flagged": avg([r["flagged"] for r in g])})
            continue
        if view == "diff":     # remove the static sky; movers show as +/- pairs
            arrs = arrs - np.nanmedian(arrs, 0)
        for i in range(0, len(g), k):
            sl = g[i:i + k]
            m = np.nanmean(arrs[i:i + k], 0)
            out.append({"img": to_png(m, view == "diff"), "rms": rms(m),
                        "t": sl[0]["t"] + (f" (avg of {len(sl)})" if len(sl) > 1 else ""), "det": d,
                        "wave": avg([r["wave"] for r in sl]), "flux": avg([r["flux"] for r in sl]),
                        "flagged": avg([r["flagged"] for r in sl])})
    return out


@app.get("/api/frames")
def frames(ra: str = None, dec: str = None, name: str = None, pmra: float = 0, pmdec: float = 0,
           size: float = 0.1, det: int = 0, n: int = 20, view: str = "frames", bin: int = 1):
    size, n, bin = min(max(size, 0.02), 0.5), min(max(n, 3), 60), min(max(bin, 1), 16)
    ra_d, dec_d = parse_position(ra, dec, name, pmra, pmdec)
    raw, total, src = get_raw(ra_d, dec_d, size, det, n)
    fr = render(raw, view, bin)
    mid = {1: 0.93, 2: 1.37, 3: 2.03, 4: 3.12, 5: 4.12, 6: 4.71}   # band centres (um), used only if a wavelength lookup fails
    sp = sorted([(f["wave"] if f["wave"] is not None else mid[f["det"]], f["flux"], f["det"], f["wave"] is None)
                 for f in fr if f["flux"] is not None])
    return {"ra": ra_d, "dec": dec_d, "size": size, "source": src, "available": total, "frames": fr,
            "spectrum": {"wave": [p[0] for p in sp], "flux": [p[1] for p in sp], "det": [p[2] for p in sp],
                         "approx": any(p[3] for p in sp)}}


def num(x):
    return None if np.ma.is_masked(x) else float(x)


IRSA_TAP = "https://irsa.ipac.caltech.edu/TAP"
GAIA_TAP = "https://gea.esac.esa.int/tap-server/tap"


def tap(url, adql, tries=3):
    """Run an ADQL query on a TAP service, retrying on dropped connections."""
    err = None
    for i in range(tries):
        try:
            return pyvo.dal.TAPService(url).run_sync(adql).to_table()
        except Exception as e:
            err = e
            time.sleep(1.5 * (i + 1))
    raise err


def _catalog_live(ra, dec, size, cat):
    rad = size * 0.75
    cone = f"CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra}, {dec}, {rad}))=1"
    try:
        if cat == "wise":      # AllWISE source catalog (IRSA TAP), W1 magnitude
            t = tap(IRSA_TAP, f"SELECT TOP 400 ra, dec, w1mpro FROM allwise_p3as_psd WHERE {cone} ORDER BY w1mpro")
            rows = [(num(r["ra"]), num(r["dec"]), num(r["w1mpro"])) for r in t]
        else:                  # Gaia DR3 (ESA archive TAP first, VizieR as fallback), G magnitude
            try:
                t = tap(GAIA_TAP, f"SELECT TOP 400 ra, dec, phot_g_mean_mag FROM gaiadr3.gaia_source "
                                  f"WHERE {cone} ORDER BY phot_g_mean_mag")
                rows = [(num(r["ra"]), num(r["dec"]), num(r["phot_g_mean_mag"])) for r in t]
            except Exception:
                v = Vizier(columns=["RA_ICRS", "DE_ICRS", "Gmag"], row_limit=400)
                t = v.query_region(SkyCoord(ra * u.deg, dec * u.deg), radius=rad * u.deg, catalog="I/355/gaiadr3")[0]
                rows = [(num(r["RA_ICRS"]), num(r["DE_ICRS"]), num(r["Gmag"])) for r in t]
    except Exception as e:
        raise RuntimeError(f"{cat} catalog query failed: {str(e)[:160]}")
    return rows


@app.get("/api/catalog")
def catalog(ra: float, dec: float, size: float = 0.1, cat: str = "gaia"):
    f = _p("cat", (cat, round(ra, 4), round(dec, 4), round(size, 4)), "json")
    if OFFLINE and f.exists():
        return {"cat": cat, "sources": json.loads(f.read_text()), "source": "saved"}
    try:
        rows = _catalog_live(ra, dec, size, cat)
    except Exception as e:
        if f.exists():
            return {"cat": cat, "sources": json.loads(f.read_text()), "source": "saved"}
        raise HTTPException(502, str(e))
    f.write_text(json.dumps(rows))
    return {"cat": cat, "sources": rows, "source": "live"}


@app.get("/api/context")
def context(ra: float, dec: float, fov: float = 0.3):
    """WISE colour picture of the field (CDS hips2fits), fetched by the server and saved."""
    f = _p("ctx", (round(ra, 4), round(dec, 4), round(fov, 3)), "jpg")
    if not (OFFLINE and f.exists()):
        try:
            r = requests.get("https://alasky.cds.unistra.fr/hips-image-services/hips2fits", timeout=40, params={
                "hips": "CDS/P/allWISE/color", "width": 640, "height": 480, "fov": fov, "projection": "TAN",
                "coordsys": "icrs", "ra": ra, "dec": dec, "format": "jpg"})
            r.raise_for_status()
            f.write_bytes(r.content)
        except Exception as e:
            if not f.exists():
                raise HTTPException(502, f"CDS image service failed: {str(e)[:120]}")
    return FileResponse(f, media_type="image/jpeg")


@app.get("/bg.jpg")
def bg():
    return FileResponse(HERE / "bg.jpg")


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


# Test targets, mirroring the preset buttons in index.html.
PRESETS = [
    dict(name="Barnard's star", pmra=-798.58, pmdec=10328.12, det=2, fov=6),
    dict(name="eps Ind B", pmra=3966, pmdec=-2536, det=5, fov=12),
    dict(ra="08h55m10.831s", dec="-07d14m42.53s", det=6, fov=6),
    dict(name="RW Aur", det=2, fov=6),
    dict(name="R Dra", det=2, fov=6),
]

if __name__ == "__main__":     # python app.py  ->  save demo data for all presets while IRSA is up
    for p in PRESETS:
        label = p.get("name") or p.get("ra")
        try:
            ra_d, dec_d = parse_position(p.get("ra"), p.get("dec"), p.get("name"), p.get("pmra", 0), p.get("pmdec", 0))
        except Exception as e:
            print("FAILED to resolve", label, e)
            continue
        size = p["fov"] / 60
        for det in (p["det"], 0):                      # the preset's band, and "All six"
            try:
                raw, total, src = get_raw(ra_d, dec_d, size, det, 20)
                print(f"saved {label}  band={det or 'all'}  {len(raw)} frames ({src})")
            except Exception as e:
                print("FAILED", label, det, e)
        for cat in ("gaia", "wise"):
            try:
                catalog(ra_d, dec_d, size, cat)
                print(f"saved {label}  {cat} catalogue")
            except Exception as e:
                print("FAILED", label, cat, e)
        try:
            context(ra_d, dec_d, max(size * 3, 0.2))
            print(f"saved {label}  WISE context image")
        except Exception as e:
            print("FAILED", label, "context image", e)
