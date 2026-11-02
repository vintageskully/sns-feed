# 작업 스케줄러가 30분마다 창 없이 실행하는 파일 (X 전용, 집 PC)
import os, sys, traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
os.environ["PC_MODE"] = "1"
log_path = os.path.join(HERE, "pc_log.txt")
try:
    if os.path.getsize(log_path) > 1_000_000:
        os.replace(log_path, log_path + ".old")
except OSError:
    pass
sys.stdout = sys.stderr = open(log_path, "a", encoding="utf-8")
print(f"\n===== {datetime.now():%Y-%m-%d %H:%M} =====", flush=True)

# GitHub에서 최신 프로그램과 계정 목록 받아오기 (실패하면 있던 걸로 실행)
RAW = "https://raw.githubusercontent.com/vintageskully/sns-feed/main/"
for name in ("feed.py", "accounts_pc.txt"):
    try:
        import urllib.request
        data = urllib.request.urlopen(RAW + name, timeout=20).read()
        if name.endswith(".py"):
            compile(data, name, "exec")  # 깨진 파일이면 교체 안 함
        path = os.path.join(HERE, name)
        if not os.path.exists(path) or open(path, "rb").read() != data:
            open(path, "wb").write(data)
            print("업데이트:", name)
    except Exception as e:
        print("업데이트 건너뜀:", name, e)
try:
    sys.path.insert(0, HERE)
    import feed
    feed.main()
except SystemExit as e:
    print("종료:", e)
except Exception:
    traceback.print_exc()
