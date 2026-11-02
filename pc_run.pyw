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
try:
    sys.path.insert(0, HERE)
    import feed
    feed.main()
except SystemExit as e:
    print("종료:", e)
except Exception:
    traceback.print_exc()
