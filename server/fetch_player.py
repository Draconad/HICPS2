"""Docker build step: bundle the hls.js web video player (Chrome/Edge/Firefox need it for the camera;
Safari and the iPhone app play the stream natively). Never fails the build - the dashboard falls back to cdnjs."""
import urllib.request

URLS = ["https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js",
        "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js"]
for url in URLS:
    try:
        data = urllib.request.urlopen(url, timeout=30).read()
        if len(data) > 100_000 and b"Hls" in data:
            open("app/static/hls.min.js", "wb").write(data)
            print(f"bundled hls.js from {url} ({len(data) // 1024} KB)")
            break
    except Exception as e:  # noqa: BLE001
        print(f"couldn't fetch {url}: {e}")
else:
    print("hls.js not bundled - the dashboard will load it from cdnjs")
