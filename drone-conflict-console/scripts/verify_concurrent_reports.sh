#!/usr/bin/env bash
# 双进程共库并发裁决验证（仅需 bash + curl + python3）。
#
# 启动两个独立的 OS 进程（uvicorn 服务），让它们指向同一个 SQLite 文件；
# 再并发对同一架次提交修订 3 与修订 2，验证存储层按数据库级原子事务裁决：
#   * 若 3 先提交：2 返回 409 stale_revision，detail.current_revision == 3；
#   * 若 2 先提交：3 仍被接受（2 为正常接受，随后被 3 覆盖）；
#   两次请求结束后批次详情 / 页面 / 预警中的实际偏移恒为最高修订（3）对应值。
#
# 用法：
#   scripts/verify_concurrent_reports.sh             # 本地两个 uvicorn 共库
#   scripts/verify_concurrent_reports.sh --docker    # Docker Compose 双实例
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT1=8011
PORT2=8012
WORK="$(mktemp -d)"

wait_health() {
  for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$1/api/health" >/dev/null && return 0
    sleep "${2:-0.5}"
  done
  echo "等待端口 $1 上的服务超时" >&2
  return 1
}

finish() {
  if [ -n "${DB_DIR:-}" ]; then
    kill "$(cat "$DB_DIR/p1.pid")" "$(cat "$DB_DIR/p2.pid")" 2>/dev/null
    rm -rf "$DB_DIR"
  fi
  rm -rf "$WORK"
}
trap finish EXIT

put_report() { # port response-body-file payload-file
  curl -s -o "$2" -w '%{http_code}' -X PUT \
    "http://127.0.0.1:$1/api/batches/$BID/reports" \
    -H 'Content-Type: application/json' --data @"$3" </dev/null
}

if [ "${1:-}" = "--docker" ]; then
  cd "$ROOT"
  docker compose --profile multiproc up -d --build
  trap 'docker compose --profile multiproc down; finish' EXIT
  PORT1=8000   # compose 默认的 web 服务
  PORT2=8012   # multiproc profile 额外启动的 web2 服务
  wait_health "$PORT1" 1 || exit 1
  wait_health "$PORT2" 1 || exit 1
else
  command -v uvicorn >/dev/null || {
    echo "未找到 uvicorn：先 pip install -r requirements.txt，或改用 --docker" >&2
    exit 2
  }
  DB_DIR="$(mktemp -d)"
  DB_FILE="$DB_DIR/console.db"
  ( cd "$ROOT" && DCC_DB_PATH="$DB_FILE" \
    uvicorn app.main:app --host 127.0.0.1 --port "$PORT1" ) >"$DB_DIR/p1.log" 2>&1 &
  echo $! >"$DB_DIR/p1.pid"
  ( cd "$ROOT" && DCC_DB_PATH="$DB_FILE" \
    uvicorn app.main:app --host 127.0.0.1 --port "$PORT2" ) >"$DB_DIR/p2.log" 2>&1 &
  echo $! >"$DB_DIR/p2.pid"
  wait_health "$PORT1" || { echo "--- p1.log ---" >&2; cat "$DB_DIR/p1.log" >&2; exit 1; }
  wait_health "$PORT2" || { echo "--- p2.log ---" >&2; cat "$DB_DIR/p2.log" >&2; exit 1; }
fi

BASE1="http://127.0.0.1:$PORT1"
BASE2="http://127.0.0.1:$PORT2"

curl -s -X POST "$BASE1/api/import" -H 'Content-Type: application/json' \
  --data @"$ROOT/examples/crossroads.json" >"$WORK/import.json"
SID=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["scenario_id"])' "$WORK/import.json")
curl -s -X POST "$BASE1/api/scenarios/$SID/solve" -H 'Content-Type: application/json' \
  -d '{"time_limit":20}' >"$WORK/solve.json"
curl -s -X POST "$BASE1/api/scenarios/$SID/batches" -H 'Content-Type: application/json' \
  -d '{"name":"并发验证批次"}' >"$WORK/batch.json"
BID=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["batch_id"])' "$WORK/batch.json")
echo "scenario=$SID batch=$BID；两个进程共库：$BASE1 与 $BASE2"

# 已有修订 1（实际偏移 7）。
echo '{"sortie_id":"UAV-A","actual_offset":7,"revision":1}' >"$WORK/r1.json"
code=$(put_report "$PORT1" "$WORK/o1.json" "$WORK/r1.json")
echo "首报修订1: HTTP $code $(cat "$WORK/o1.json")"

# 两个进程在同一瞬间分别提交修订 3（进程1）与修订 2（进程2）。
echo '{"sortie_id":"UAV-A","actual_offset":30,"revision":3}' >"$WORK/r3.json"
echo '{"sortie_id":"UAV-A","actual_offset":20,"revision":2}' >"$WORK/r2.json"
put_report "$PORT1" "$WORK/o3.json" "$WORK/r3.json" >"$WORK/c3" &
CPID1=$!
put_report "$PORT2" "$WORK/o2.json" "$WORK/r2.json" >"$WORK/c2" &
CPID2=$!
# 只等这两个并发请求，不能裸 wait（会一并等待长驻的 uvicorn 服务进程）。
wait "$CPID1" "$CPID2"
echo "修订3(进程1): HTTP $(cat "$WORK/c3") $(cat "$WORK/o3.json")"
echo "修订2(进程2): HTTP $(cat "$WORK/c2") $(cat "$WORK/o2.json")"

# 裁决断言：修订3必接受；修订2视提交先后为接受或 current_revision=3 的旧号冲突；
# 批次详情与预警最终都必须是最高修订 3 的实际偏移 30；同号同值幂等、同号异值冲突。
echo '{"sortie_id":"UAV-A","actual_offset":30,"revision":3}' >"$WORK/p_idm.json"
echo '{"sortie_id":"UAV-A","actual_offset":31,"revision":3}' >"$WORK/p_cfl.json"
put_report "$PORT1" "$WORK/o_idm.json" "$WORK/p_idm.json" >"$WORK/c_idm"
put_report "$PORT1" "$WORK/o_cfl.json" "$WORK/p_cfl.json" >"$WORK/c_cfl"
echo "同号同值重试: HTTP $(cat "$WORK/c_idm") $(cat "$WORK/o_idm.json")"
echo "同号异值冲突: HTTP $(cat "$WORK/c_cfl") $(cat "$WORK/o_cfl.json")"

if ! BID="$BID" PORT1="$PORT1" WORK="$WORK" python3 - <<'PY'
import json, os, sys, urllib.request
bid = os.environ["BID"]; base = f"http://127.0.0.1:{os.environ['PORT1']}"
work = os.environ["WORK"]

def load(name):
    with open(os.path.join(work, name)) as f:
        return json.load(f)

c3, o3 = int(open(os.path.join(work, "c3")).read()), load("o3.json")
c2, o2 = int(open(os.path.join(work, "c2")).read()), load("o2.json")
ci, oi = int(open(os.path.join(work, "c_idm")).read()), load("o_idm.json")
cc, oc = int(open(os.path.join(work, "c_cfl")).read()), load("o_cfl.json")

assert c3 == 200 and o3.get("accepted") is True, f"修订3必须被接受: {c3} {o3}"
if c2 == 200:
    assert o2.get("accepted") is True, f"2先提交时应正常接受: {o2}"
    order = "2 先提交（接受），3 随后更新"
else:
    assert c2 == 409 and o2["detail"]["reason"] == "stale_revision", \
        f"3先提交时2应为旧号冲突: {c2} {o2}"
    assert o2["detail"]["current_revision"] == 3, "冲突响应必须回传当前修订3"
    order = "3 先提交，2 收到 current_revision=3 的旧号冲突"
assert ci == 200 and oi.get("idempotent") is True, f"同号同值应幂等: {ci} {oi}"
assert cc == 409 and oc["detail"]["reason"] == "revision_value_conflict" \
    and oc["detail"]["stored_actual_offset"] == 30, \
    f"同号异值应冲突且保留30: {cc} {oc}"

with urllib.request.urlopen(f"{base}/api/batches/{bid}") as r:
    batch = json.load(r)
rep = next(r for r in batch["reports"] if r["sortie_id"] == "UAV-A")
assert (rep["revision"], rep["actual_offset"]) == (3, 30), f"批次详情未取最高修订: {rep}"
with urllib.request.urlopen(f"{base}/api/batches/{bid}/warning?t=30") as r:
    warning = json.load(r)
wrow = next(s for s in warning["warning"]["sorties"] if s["id"] == "UAV-A")
assert (wrow["revision"], wrow["actual_offset"], wrow["takeoff_offset"]) == (3, 30, 30), \
    f"预警未使用最高修订对应偏移: {wrow}"
dev = next(x for x in warning["warning"]["reported_deviations"] if x["sortie_id"] == "UAV-A")
assert (dev["revision"], dev["actual_offset"], dev["deviation_seconds"]) == (3, 30, 26), \
    f"偏离明细未按最高修订: {dev}"
print(f"裁决顺序：{order}")
print("批次详情与预警均使用最高修订 3 的实际偏移 30（计划 4，偏离 +26s）：OK")
PY
then
  echo "并发裁决断言失败，请检查上方输出" >&2
  exit 1
fi
echo "全部断言通过。"
