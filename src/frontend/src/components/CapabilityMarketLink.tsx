import { Icon } from "./Icon";
export function CapabilityMarketLink({ compact=false }: {compact?:boolean}) {
 return <section aria-label="DataClawHub 平台" className={`${compact ? "mb-3 p-3" : "mx-4 mt-4 shrink-0 p-4 sm:mx-8"} rounded-card border border-[#f0a06b] bg-card`}>
  <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
   <div className="min-w-0 flex-1 basis-[280px]">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
     <img src="/dataclawhub-logo.png" alt="DataClawHub" width={180} height={45} className="h-auto w-[180px] shrink-0" />
     <p className="text-[13px] font-medium text-[#245dca]">物流行业 AI 协作社区</p>
    </div>
    <p className="mt-1.5 text-[12px] leading-relaxed text-ink-2">连接物流行业的任务协作、数据成果、行业知识与开放工具。获取 Skill 或 MCP 配置，再回到这里导入使用。</p>
   </div>
   <a className="btn btn-outline shrink-0 border-[#c2d3f4] text-[#245dca] hover:border-[#245dca] hover:bg-[#f2f6ff]" href="https://dataclawhub.com/login?mode=register&ref=64VXUD" target="_blank" rel="noopener noreferrer">访问官网<Icon name="external" size={14}/></a>
   {compact && <a className="btn btn-outline shrink-0" href="https://dataclawhub.com/toolkits/skills" target="_blank" rel="noopener noreferrer">浏览工具市场<Icon name="external" size={14}/></a>}
  </div>
 </section>;
}
