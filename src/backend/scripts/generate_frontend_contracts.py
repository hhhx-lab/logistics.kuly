"""由实际OpenAPI生成请求、参数与已发布响应类型；响应空schema不伪造，--check检测漂移。"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

BACKEND=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BACKEND))
from app.api.main import create_app

OUTPUT=BACKEND.parent/'frontend/src/api/generated/request-contracts.ts'


def refs(schema):
    if isinstance(schema,dict):
        if '$ref' in schema:yield schema['$ref'].rsplit('/',1)[-1]
        for value in schema.values():yield from refs(value)
    elif isinstance(schema,list):
        for value in schema:yield from refs(value)


def ts(schema):
    if not schema:return 'unknown'
    if '$ref' in schema:return schema['$ref'].rsplit('/',1)[-1]
    if 'const' in schema:return json.dumps(schema['const'],ensure_ascii=False)
    if 'enum' in schema:return ' | '.join(json.dumps(value,ensure_ascii=False) for value in schema['enum'])
    for key,join in [('anyOf',' | '),('oneOf',' | '),('allOf',' & ')]:
        if key in schema:return join.join('('+ts(value)+')' for value in schema[key])
    kind=schema.get('type')
    if isinstance(kind,list):return ' | '.join(ts({**schema,'type':value}) for value in kind)
    if kind=='null':return 'null'
    if kind=='string':return 'string'
    if kind in {'number','integer'}:return 'number'
    if kind=='boolean':return 'boolean'
    if kind=='array':
        if 'prefixItems' in schema:
            return '['+', '.join(ts(value) for value in schema['prefixItems'])+']'
        return '('+ts(schema.get('items',{}))+')[]'
    if kind=='object' or 'properties' in schema:
        required=set(schema.get('required',[]));properties=schema.get('properties',{})
        fields=[json.dumps(name)+('' if name in required else '?')+': '+ts(value)+';' for name,value in properties.items()]
        additional=schema.get('additionalProperties',True)
        if additional is not False:
            extra=ts(additional) if isinstance(additional,dict) else 'unknown'
            fields.append('[key: string]: '+extra+';')
        return '{ '+' '.join(fields)+' }'
    raise ValueError('不支持的OpenAPI schema结构：'+json.dumps(schema,ensure_ascii=False))


def published_response(schema, schemas, seen=frozenset()):
    """裸 dict 不代表已发布字段契约；204/文件响应不伪造 JSON 类型。"""
    if not isinstance(schema, dict):return False
    if "$ref" in schema:
        name=schema["$ref"].rsplit("/",1)[-1]
        return name not in seen and published_response(schemas[name], schemas, seen | {name})
    if schema.get("properties"):return True
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:return all(published_response(item, schemas, seen) for item in schema[key])
    if schema.get("type") == "array":return published_response(schema.get("items"), schemas, seen)
    return schema.get("type") in {"string", "number", "integer", "boolean", "null"}


def render(document):
    schemas=document['components']['schemas'];needed=set();operations=[];responses=[];responses_untyped=[]
    for path,methods in sorted(document['paths'].items()):
        for method,operation in sorted(methods.items()):
            if method not in {'get','post','put','patch','delete'}:continue
            body=operation.get('requestBody',{}).get('content',{}).get('application/json',{}).get('schema')
            parameters=operation.get('parameters',[])
            parts=[]
            if body is not None:
                needed.update(refs(body));parts.append('body'+('' if operation['requestBody'].get('required') else '?')+': '+ts(body)+';')
            for location in ('path','query'):
                selected=[value for value in parameters if value['in']==location]
                if selected:
                    shape={'type':'object','additionalProperties':False,'properties':{value['name']:value['schema'] for value in selected},
                        'required':[value['name'] for value in selected if value.get('required')]}
                    needed.update(refs(shape));parts.append(location+': '+ts(shape)+';')
            operations.append('  '+json.dumps(method.upper()+' '+path)+': { '+' '.join(parts)+' };')
            success={status:value.get('content',{}).get('application/json',{}).get('schema')
                for status,value in operation.get('responses',{}).items() if str(status).startswith('2')}
            typed={status:schema for status,schema in success.items() if published_response(schema, schemas)}
            if typed:
                for schema in typed.values():needed.update(refs(schema))
                responses.append('  '+json.dumps(method.upper()+' '+path)+': { '+
                    ' '.join(json.dumps(status)+': '+ts(schema)+';' for status,schema in typed.items())+' };')
            if any(not published_response(schema, schemas) for schema in success.values()):
                responses_untyped.append(method.upper()+' '+path)
    pending=list(needed)
    while pending:
        name=pending.pop()
        for child in refs(schemas[name]):
            if child not in needed:needed.add(child);pending.append(child)
    lines=['// 由 src/backend/scripts/generate_frontend_contracts.py 从实际OpenAPI生成，请勿手改。',
        '// 请求/参数和已发布响应；required、nullable与可选default按OpenAPI。数值/长度约束仍由Pydantic验证。',
        '// ApiResponses只包含有字段契约的响应；其余接口列在末尾，不把裸dict声称为已类型化。','']
    lines.extend('export type '+name+' = '+('string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue }' if name == 'JsonValue' else ts(schemas[name]))+';' for name in sorted(needed))
    lines.extend(['','export interface ApiRequests {',*operations,'}','','export interface ApiResponses {',*responses,'}','',
        'export const untypedResponseOperations = '+json.dumps(responses_untyped,ensure_ascii=False,indent=2)+' as const;',''])
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    result=render(create_app().openapi())
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text()!=result:
            print('OpenAPI契约已漂移，请运行 npm run contracts:generate',file=sys.stderr);return 1
        print('OpenAPI请求／参数／已发布响应契约无漂移');return 0
    OUTPUT.parent.mkdir(parents=True,exist_ok=True);OUTPUT.write_text(result)
    print('已生成请求／参数／已发布响应TypeScript；未类型化响应仍需逐项发布模型');return 0


if __name__=='__main__':raise SystemExit(main())
