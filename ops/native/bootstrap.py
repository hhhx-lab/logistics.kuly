"""原生部署初始化：读取私有 env，迁移、授权和创建独立部署账号。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

PROJECT = Path(__file__).resolve().parents[2]
BACKEND = PROJECT / 'src/backend'
CONFIG = Path('/opt/logistics-agent/config')
sys.path.insert(0, str(BACKEND))


def load_env(name: str) -> None:
    for key, value in dotenv_values(CONFIG / name).items():
        if value is not None:
            os.environ[key] = value


def grant_runtime() -> None:
    secrets = json.loads((CONFIG / 'server-bootstrap.json').read_text())
    role_sql = ''
    for role, key in (('logistics_api_runtime', 'api_password'), ('logistics_worker_runtime', 'worker_password')):
        password = secrets[key].replace("'", "''")
        role_sql += f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN
            CREATE ROLE {role} LOGIN PASSWORD '{password}'
              NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
          END IF;
        END $$;
        GRANT logistics_app, logistics_worker TO {role};
        """
    # 跨租户查询能力仅供既有 SECURITY DEFINER 函数，角色本身不可登录。
    sql = '''
    GRANT SELECT ON tenants, users TO logistics_worker;
    GRANT SELECT ON public.alembic_version TO logistics_worker;
    GRANT SELECT,INSERT,UPDATE,DELETE ON checkpoint_migrations, checkpoints,
        checkpoint_blobs, checkpoint_writes TO logistics_checkpoint;
    GRANT SELECT ON runs, assistant_continuations, conversation_turns,
        case_deletions TO logistics_lookup;
    ALTER FUNCTION worker_tenant_of_run(uuid) OWNER TO logistics_lookup;
    ALTER FUNCTION worker_tenant_of_continuation(uuid) OWNER TO logistics_lookup;
    ALTER FUNCTION worker_tenant_of_turn(uuid) OWNER TO logistics_lookup;
    ALTER FUNCTION worker_tenant_of_deletion(uuid) OWNER TO logistics_lookup;
    '''
    subprocess.run(
        ['sudo', '-n', '-u', 'postgres', 'psql', '-d', 'logistics',
         '-v', 'ON_ERROR_STOP=1', '-q'],
        input=role_sql + sql, text=True, check=True, stdout=subprocess.DEVNULL,
    )
    for name, original, runtime in (
        ('api.env', 'logistics_app', 'logistics_api_runtime'),
        ('worker.env', 'logistics_worker', 'logistics_worker_runtime'),
    ):
        path = CONFIG / name
        path.write_text(path.read_text().replace('://' + original + ':', '://' + runtime + ':'))
        path.chmod(0o600)


async def initialize_bucket() -> None:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    from app.config import get_settings

    settings = get_settings()
    client = boto3.client(
        's3', endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        config=Config(signature_version='s3v4'),
    )
    try:
        try:
            client.head_bucket(Bucket=settings.s3_bucket)
        except ClientError as exc:
            code = str(exc.response['Error']['Code'])
            if code not in {'404', 'NoSuchBucket', 'NotFound'}:
                raise
            client.create_bucket(Bucket=settings.s3_bucket)
        client.put_bucket_versioning(
            Bucket=settings.s3_bucket,
            VersioningConfiguration={'Status': 'Enabled'},
        )
        # 同源桶路径无需跨域 CORS。桶匿名访问保持禁用。
    finally:
        client.close()


async def initialize_admin() -> None:
    from sqlalchemy import select
    from app.config import get_settings
    from app.identity.models import Tenant, User
    from app.identity.seed import FULL_PERMISSIONS
    from app.identity.service import hash_password
    from app.infra.database import create_database_engine, create_session_factory

    load_env('admin.env')
    account = os.environ['DEPLOY_ADMIN_ACCOUNT']
    engine = create_database_engine(get_settings())
    try:
        async with create_session_factory(engine)() as db, db.begin():
            existing = (await db.execute(select(User).where(User.account == account))).scalar_one_or_none()
            if existing is not None:
                return
            tenant = Tenant(name='行业智能体项目')
            db.add(tenant)
            await db.flush()
            db.add(User(
                tenant_id=tenant.id, account=account,
                display_name=os.environ['DEPLOY_ADMIN_DISPLAY_NAME'],
                password_hash=hash_password(os.environ['DEPLOY_ADMIN_PASSWORD']),
                permissions=FULL_PERMISSIONS, is_active=True,
            ))
    finally:
        await engine.dispose()


def main() -> None:
    load_env('server.env')
    load_env('migration.env')
    os.chdir(BACKEND)
    sys.path.insert(0, str(PROJECT / 'ops'))
    from runtime import migrate
    migrate()
    grant_runtime()
    asyncio.run(initialize_bucket())
    asyncio.run(initialize_admin())
    print('迁移、检查点、私有版本化桶和独立管理员初始化完成。')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # 连接串、口令和供应商错误正文不得进入部署日志。
        print('部署初始化失败：' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
