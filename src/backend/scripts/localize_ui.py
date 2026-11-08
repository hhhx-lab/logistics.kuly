"""构建时生成本地界面词典；不发送用户单票或配置。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_settings, ModelRole
from app.infra.model_gateway import build_model_gateway, CallBudget, Succeeded
from app.prompts.registry import PromptRegistry
from app.prompts.schemas import UiLocalizationResult

ROOT = Path(__file__).resolve().parents[2] / 'frontend/src/i18n'

async def main():
    registry = PromptRegistry.load()
    gateway = build_model_gateway(get_settings())
    binding = gateway.binding_for(ModelRole.CHAT)
    source = json.loads((ROOT / 'source.json').read_text())
    queue = asyncio.Semaphore(2)
    async def language(code):
        output = ROOT / (code + '.json')
        saved = json.loads(output.read_text()) if output.exists() else {}
        missing = [value for value in source if value not in saved]
        batches, batch, length = [], [], 0
        for value in missing:
            if batch and (length + len(value) > 1500 or len(batch) >= 80):
                batches.append(batch); batch, length = [], 0
            batch.append(value); length += len(value)
        if batch: batches.append(batch)
        for index, batch in enumerate(batches):
            entries = {str(i): value for i, value in enumerate(batch)}
            async with queue:
                prompt = registry.get('ui.localize').render(target_language=code, entries=entries)
                result = await gateway.structured_generate(binding, [prompt], UiLocalizationResult,
                    f'ui-localize-{code}-{index}', budget=CallBudget(max_calls=2, max_total_tokens=32000))
            if not isinstance(result, Succeeded) or result.is_fake:
                print(code, index, '未成功', type(result).__name__, getattr(result, 'kind', None), flush=True)
                break
            validated = {batch[int(key)]: value for key, value in result.value.entries.items()
                if key in entries and value.strip()}
            saved.update(validated)
            current = json.loads(output.read_text()) if output.exists() else {}
            saved = {**saved, **{key: value for key, value in current.items() if key not in validated}}
            output.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + '\n')
            print(code, index + 1, len(batches), '累计', len(saved), '缺漏', len(batch)-len(validated), flush=True)
        return code, len(saved), len(source)
    try:
        print(await asyncio.gather(*(language(code) for code in (sys.argv[1:] or ['en', 'fr', 'es', 'ru']))), flush=True)
    finally:
        await gateway.aclose()

if __name__ == '__main__':
    asyncio.run(main())
