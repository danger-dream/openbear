#!/usr/bin/env bash
# Geekbench 5 for OpenBear. The free edition must upload its result to browser.geekbench.com;
# the scores are read back from that page. Geekbench needs about 1.5 GB of memory, so a
# smaller machine gets a temporary swap file for the run.
set -u
out=${OB_BENCH_OUT:?}
gb=${OB_BENCH_DIR:?}/bin/geekbench/geekbench5
swapfile=
cleanup() {
  if [ -n "$swapfile" ]; then
    swapoff "$swapfile" 2>/dev/null
    rm -f "$swapfile"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
swapfree=$(awk '/^SwapFree:/ {print $2}' /proc/meminfo)
if [ $(( ${avail:-0} + ${swapfree:-0} )) -lt 1500000 ]; then
  swapfile=/var/tmp/openbear-geekbench.swap
  echo "可用内存不足 1.5 GB，临时创建 2 GB swap：$swapfile（结束后删除）"
  rm -f "$swapfile"
  if ! { { fallocate -l 2G "$swapfile" 2>/dev/null || dd if=/dev/zero of="$swapfile" bs=1M count=2048 status=none; } &&
      chmod 600 "$swapfile" && mkswap "$swapfile" >/dev/null && swapon "$swapfile"; }; then
    echo "临时 swap 创建失败，按现有内存继续"
    rm -f "$swapfile"
    swapfile=
  fi
fi

oom_before=$(awk '$1 == "oom_kill" {print $2}' /proc/vmstat 2>/dev/null)
"$gb" --upload 2>&1 | awk '{gsub(/\/claim\?key=[^[:space:]]+/, ""); print; fflush()}' | tee "$out/geekbench.log"
rc=${PIPESTATUS[0]}
oom_after=$(awk '$1 == "oom_kill" {print $2}' /proc/vmstat 2>/dev/null)
oom=false
[ "${oom_after:-0}" -gt "${oom_before:-0}" ] && oom=true

url=$(grep -oE 'https://browser\.geekbench\.com/v5/cpu/[0-9]+' "$out/geekbench.log" | head -n 1)
single= multi=
if [ -n "$url" ] && curl -fsSL --connect-timeout 10 --max-time 30 "$url.csv" -o "$out/scores.csv" 2>/dev/null; then
  field() { awk -F, -v key="$1" '{gsub(/"/, ""); if ($1 == key) {print $2; exit}}' "$out/scores.csv"; }
  single=$(field Single-Core)
  multi=$(field Multi-Core)
fi

{
  printf '\033[1;36mGeekbench 5\033[0m\n'
  printf '单核成绩：\033[1;32m%s\033[0m\n' "${single:-未取得}"
  printf '多核成绩：\033[1;32m%s\033[0m\n' "${multi:-未取得}"
  printf '结果页面：%s\n' "${url:-未取得}"
  [ "$oom" = true ] && printf '\033[1;31m运行期间发生 OOM，成绩可能偏低或无效\033[0m\n'
  printf '\n'
  cat "$out/geekbench.log"
} > "$out/report-4.ansi"
printf '{"url":"%s","single":"%s","multi":"%s","oom":%s}\n' "$url" "$single" "$multi" "$oom" > "$out/result-4.json"
rm -f "$out/geekbench.log" "$out/scores.csv"
exit "$rc"
