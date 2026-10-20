#!/usr/bin/env bash
# GPU wheel 并行下载：torch 大文件分片并行，小 wheel 直接下，全部 zip 校验
set -u
cd /f/retrochimera/desktop/dist-pkg || exit 1
WHEELS_DIR=data/wheels-gpu
PY=/f/retrochimera/desktop/dist-pkg/python39/python.exe
mkdir -p "$WHEELS_DIR"
rm -f "$WHEELS_DIR"/*.whl

TORCH_URL="https://download-r2.pytorch.org/whl/cu121/torch-2.2.2%2Bcu121-cp39-cp39-win_amd64.whl"
TORCH_NAME="torch-2.2.2+cu121-cp39-cp39-win_amd64.whl"
N=6

echo "=== 获取 torch 总大小 ==="
# -r 0-0 让服务端在 Content-Range 里返回总字节数
cl=$(curl -sI -r 0-0 --connect-timeout 30 "$TORCH_URL" | tr -d '\r' | awk -F'/' '/[Cc]ontent-Range/{print $2}' | tr -d ' ')
if [ -z "$cl" ]; then
  cl=$(curl -sI --connect-timeout 30 "$TORCH_URL" | tr -d '\r' | awk -F': ' '/[Cc]ontent-[Ll]ength/{print $2}' | tr -d ' ')
fi
echo "torch 总字节: $cl"
[ -z "$cl" ] && { echo "!! 取不到 torch 大小"; exit 1; }

ps=$(( (cl + N - 1) / N ))
echo "分 $N 片，每片约 $(( ps/1024/1024 )) MB"
tmp=$(mktemp -d)
pids=()
for i in $(seq 0 $((N-1))); do
  start=$(( i*ps ))
  end=$(( start + ps - 1 ))
  [ $end -ge $cl ] && end=$(( cl - 1 ))
  out="$tmp/part_$i"
  (
    for try in 1 2 3 4 5; do
      curl -s -L --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 30 \
           -r ${start}-${end} -o "$out" "$TORCH_URL" && break
      echo "  part $i 第${try}次失败，重试"
      sleep 2
    done
  ) &
  pids+=($!)
done
echo "已启动 $N 个并行分片下载…"
fail=0
for p in "${pids[@]}"; do wait $p || fail=1; done
if [ $fail -ne 0 ]; then echo "!! 有分片下载失败"; exit 1; fi

# 拼接
echo "=== 拼接分片 ==="
cat "$tmp"/part_* > "$WHEELS_DIR/$TORCH_NAME"
rm -rf "$tmp"
final=$(stat -c%s "$WHEELS_DIR/$TORCH_NAME")
echo "拼接完成 size=$final / $cl"
[ "$final" != "$cl" ] && { echo "!! 大小不符，重下"; exit 1; }

echo "=== 下载其余 4 个小 wheel ==="
declare -a small=(
"https://download-r2.pytorch.org/whl/cu121/torchvision-0.17.2%2Bcu121-cp39-cp39-win_amd64.whl|torchvision-0.17.2+cu121-cp39-cp39-win_amd64.whl"
"https://data.pyg.org/whl/torch-2.2.0%2Bcu121/torch_scatter-2.1.2%2Bpt22cu121-cp39-cp39-win_amd64.whl|torch_scatter-2.1.2+pt22cu121-cp39-cp39-win_amd64.whl"
"https://data.pyg.org/whl/torch-2.2.0%2Bcu121/torch_sparse-0.6.18%2Bpt22cu121-cp39-cp39-win_amd64.whl|torch_sparse-0.6.18+pt22cu121-cp39-cp39-win_amd64.whl"
"https://data.pyg.org/whl/torch-2.2.0%2Bcu121/torch_cluster-1.6.3%2Bpt22cu121-cp39-cp39-win_amd64.whl|torch_cluster-1.6.3+pt22cu121-cp39-cp39-win_amd64.whl"
)
for entry in "${small[@]}"; do
  url="${entry%%|*}"; name="${entry##*|}"
  for try in 1 2 3 4 5 6; do
    curl -s -L --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 30 -o "$WHEELS_DIR/$name" "$url" && break
    echo "  $name 第${try}次失败，重试"; sleep 2
  done
done

echo "=== 完整性校验（zip）==="
for whl in "$WHEELS_DIR"/*.whl; do
  "$PY" -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); bad=z.testzip(); print(sys.argv[1].split('/')[-1], 'OK' if bad is None else ('BAD:'+str(bad)))" "$whl" || { echo "!! $whl 校验失败"; exit 1; }
done
echo "ALL_WHEELS_DONE"
