#!/bin/bash
# LocalStack 就绪后自动执行：建桶并设置浏览器直传所需的 CORS。
# 桶保持私有，所有读取都经后端鉴权代理，浏览器只拿短期签名 PUT。
set -euo pipefail

BUCKET=logistics-documents

awslocal s3api create-bucket --bucket "$BUCKET" 2>/dev/null || true

# 开启版本控制：整票删除时必须能删除所有 object versions 与 delete markers，
# 这是方案 4.4 节要求可验证的清理能力。
awslocal s3api put-bucket-versioning \
  --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

# 浏览器直传需要 CORS 放行 PUT，并暴露 ETag 供 U05 验收比对。
# AllowedOrigin 只给本地前端地址，不用通配符。
awslocal s3api put-bucket-cors --bucket "$BUCKET" --cors-configuration '{
  "CORSRules": [
    {
      "AllowedOrigins": ["http://localhost:5173", "http://127.0.0.1:5173"],
      "AllowedMethods": ["PUT", "GET", "HEAD"],
      "AllowedHeaders": ["*"],
      "ExposeHeaders": ["ETag"],
      "MaxAgeSeconds": 3000
    }
  ]
}'

echo "S3 初始化完成：桶 $BUCKET 已建好，已开启版本控制与直传 CORS"
