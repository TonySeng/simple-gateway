#!/usr/bin/env bash
# OCR 中转服务 · curl 调用示例
#
# 用法：
#   export OCR_BASE_URL=http://127.0.0.1:8010
#   export OCR_API_KEY=ocr_sk_xxxxxxxxxxxxxxxx
#   bash demo_curl.sh ./test.png
set -euo pipefail

BASE_URL="${OCR_BASE_URL:-http://127.0.0.1:8010}"
API_KEY="${OCR_API_KEY:-}"
IMAGE="${1:-./test.png}"

if [ -z "$API_KEY" ]; then
  echo "请先设置环境变量 OCR_API_KEY（在管理后台「API Key 管理」中创建）" >&2
  exit 1
fi
if [ ! -f "$IMAGE" ]; then
  echo "图片不存在：$IMAGE" >&2
  exit 1
fi

echo "== 方式一：文件上传 =="
curl -sS -X POST "${BASE_URL}/v1/ocr/recognize/file" \
  -H "Authorization: Bearer ${API_KEY}" \
  -F "file=@${IMAGE}" \
  -F "engine=" \
  | python -c "import sys,json;d=json.load(sys.stdin);print(json.dumps({k:d.get(k) for k in ('provider','engine','cost_ms','char_count','text')},ensure_ascii=False,indent=2))"

echo
echo "== 方式二：base64 JSON =="
B64=$(base64 -w0 "$IMAGE" 2>/dev/null || base64 "$IMAGE" | tr -d '\n')
curl -sS -X POST "${BASE_URL}/v1/ocr/recognize" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"image_base64\":\"${B64}\"}" \
  | python -c "import sys,json;d=json.load(sys.stdin);print(json.dumps({k:d.get(k) for k in ('provider','engine','cost_ms','char_count','text')},ensure_ascii=False,indent=2))"

echo
echo "== 方式三：图片 URL =="
curl -sS -X POST "${BASE_URL}/v1/ocr/recognize" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"https://example.com/test.png"}'

echo
echo "== 附：查看服务支持的提供商类型 =="
curl -sS "${BASE_URL}/v1/ocr/types" -H "Authorization: Bearer ${API_KEY}"
echo
