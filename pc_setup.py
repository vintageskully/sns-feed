"""집 PC(윈도우)에 X 알림을 설치. pc_setup.bat 이 실행함."""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
py = sys.executable
pyw = os.path.join(os.path.dirname(py), "pythonw.exe")


def step(msg):
    print("\n▶ " + msg, flush=True)


def run(cmd):
    print("  " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


step("1/5 필요한 프로그램 설치 (2~5분)")
run([py, "-m", "pip", "install", "--upgrade", "-q", "-r", "requirements.txt"])
run([py, "-m", "playwright", "install", "chromium"])

step("2/5 텔레그램 봇 토큰")
cfg_path = os.path.join(HERE, "pc_config.json")
cfg = {}
if os.path.exists(cfg_path):
    cfg = json.load(open(cfg_path, encoding="utf-8"))
if cfg.get("token"):
    print("  이미 저장된 토큰을 사용합니다. (바꾸려면 pc_config.json 을 지우고 다시 실행)")
else:
    tok = input("  @BotFather 에서 복사한 봇 토큰을 붙여넣고 Enter: ").strip()
    cfg["token"] = tok
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"))

step("3/5 X 로그인 (창이 열리면 로그인하고, 끝나면 그 창을 닫으세요)")
env = dict(os.environ, PC_MODE="1")
subprocess.run([py, "feed.py", "login"], env=env, check=True)

step("4/5 30분마다 자동 실행 + PC 켤 때 자동 실행 등록")
run(["schtasks", "/create", "/f", "/tn", "SNS-X-Feed", "/sc", "minute", "/mo", "30",
     "/tr", f'"{pyw}" "{os.path.join(HERE, "pc_run.pyw")}"'])
startup = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
lnk = os.path.join(startup, "SNS-X-Feed.lnk")
ps = (f'$s=(New-Object -ComObject WScript.Shell).CreateShortcut("{lnk}");'
      f'$s.TargetPath="{pyw}";$s.Arguments=\'"{os.path.join(HERE, "pc_run.pyw")}"\';'
      f'$s.WorkingDirectory="{HERE}";$s.Save()')
run(["powershell", "-NoProfile", "-Command", ps])

step("5/5 첫 실행 (계정마다 최신 글 1개씩 텔레그램으로 보냄)")
env.update(SEND_LATEST="true", IGNORE_QUIET="true")
subprocess.run([py, "feed.py"], env=env)

print("\n✅ 설치 완료! 이제 PC가 켜져 있으면 30분마다 X 새 글이 텔레그램으로 옵니다.")
print("   PC를 껐다 켜면 그동안 올라온 글을 묶어서 보냅니다.")
input("\nEnter 를 누르면 창이 닫힙니다.")
