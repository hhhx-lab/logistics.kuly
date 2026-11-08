-- 数据库首次初始化时启用必需扩展。
-- pg_trgm 供文件名、地名与单号的模糊检索索引使用；方案要求它在索引迁移前就绪，
-- 若缺失则就绪检查必须明确报错，而不是让模糊检索退化为全表扫描。
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 应用与 Worker 的数据库角色不得持有 BYPASSRLS 或 DDL 权限。
-- 迁移使用独立的 owner 角色执行，运行时角色只有 DML。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'logistics_app') THEN
    CREATE ROLE logistics_app LOGIN PASSWORD 'logistics_app_dev_only' NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'logistics_worker') THEN
    CREATE ROLE logistics_worker LOGIN PASSWORD 'logistics_worker_dev_only' NOBYPASSRLS;
  END IF;
END
$$;

GRANT CONNECT ON DATABASE logistics TO logistics_app, logistics_worker;
GRANT USAGE ON SCHEMA public TO logistics_app, logistics_worker;
