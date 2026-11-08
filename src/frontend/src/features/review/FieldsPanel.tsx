import { translateUi, useInterfaceLocale, getInterfaceLocale } from "../../i18n/runtime";
import { languageName, languageCode } from "../../i18n/languages";
// 字段与明细区：按组展示，行内固定「原文 → 规范值 → 人工采用值 → 译文 → 状态与来源」。
// 未提取、识别候选、无法判断、已人工确认各有不同文字，不靠颜色表意。
import { useEffect, useMemo, useState } from "react";
import type { FieldView, LineItemView, ReviewFileNode, ReviewView, TranslationSegmentView } from "../../api/types";
import { Icon } from "../../components/Icon";
import { EmptyState, Loading, StatusPill, formatDateTime , shortId } from "../../components/Status";
import { Menu, Dialog, useToast } from "../../components/Overlay";
import { useFields, useLineItems, useReviewCommand } from "./hooks";
import { reviewApi } from "../../api/review";
import { errorText } from "../../api/client";
import { useRef } from "react";
import { useReviewPreference } from "../../stores/reviewPreference";
import { useSession } from "../../stores/session";

/**
 * 页面设计要求六个分组，契约的 FieldView.group 只有四个枚举值。
 * 「单号与引用」「翻译与质量」按字段名／角色从既有分组中细分，不改契约语义。
 */
type GroupKey = "parties" | "refs" | "logistics" | "goods" | "amounts" | "translation";

const GROUPS: { key: GroupKey; label: string }[] = [
  { key: "parties", label: "主体与联系" },
  { key: "refs", label: "单号与引用" },
  { key: "logistics", label: "运输与日期" },
  { key: "goods", label: "货物与包装" },
  { key: "amounts", label: "金额与贸易条件" },
  { key: "translation", label: "翻译与质量" },
];

const REF_PATTERN = /号|编号|No\.|参考|引用|柜号|铅封/;

const PARTY_ROLES: Record<string, string> = {
  seller: "售出方", buyer: "买入方", shipper: "发货人", consignor: "发货人",
  consignee: "收货人", sender: "寄件人", receiver: "收件人", notify_party: "通知方",
  carrier: "承运人", exporter: "出口方", importer: "进口方", manufacturer: "生产商",
  supplier: "供应商", freight_forwarder: "货运代理", signatory: "签字人",
};

export function fieldDisplayName(field: FieldView): string {
  const role = field.role ? PARTY_ROLES[field.role.toLowerCase().replace(/[\s-]+/g, "_")] : undefined;
  if (!role || field.group !== "parties") return translateUi(field.name);
  const name = field.name.replace(/^(?:售出方|买入方|卖方|买方|发货人|收货人|寄件人|收件人|通知方|承运人|出口方|进口方|生产商|供应商|货运代理|签字人)[的\s·：:]*/, "");
  return `${translateUi(role)}${getInterfaceLocale() === "zh-CN" ? "" : " "}${translateUi(name)}`;
}

function fieldTranslation(field: FieldView, segments: TranslationSegmentView[]) {
  const sourceIds = new Set(field.source_segment_ids?.length ? field.source_segment_ids : [field.target.source_segment_id]);
  return segments.find(segment => segment.document_id === field.document_id && (segment.source_segment_ids ?? [segment.source_segment_id]).some(id => sourceIds.has(id)));
}

function groupOf(f: FieldView): GroupKey {
  if (REF_PATTERN.test(f.name)) return "refs";
  if (/源语言|目标语言|质量|翻译/.test(f.name)) return "translation";
  return f.group as GroupKey;
}

const STATUS_TEXT: Record<FieldView["review_status"], { text: string; tone: "ok" | "warn" | "info" | "brand" | "dis"; icon: Parameters<typeof StatusPill>[0]["icon"] }> = {
  confirmed: { text: "已人工确认", tone: "ok", icon: "check" },
  revised: { text: "已人工修订", tone: "brand", icon: "pencil" },
  unknown: { text: "无法判断", tone: "warn", icon: "question" },
  candidate: { text: "识别候选", tone: "warn", icon: "warn" },
  unreviewed: { text: "未复核", tone: "info", icon: "clock" },
};

export interface FieldAction {
  field: FieldView;
  kind: "confirm" | "mark-unknown" | "revise";
}

export function FieldsPanel({
  view,
  file,
  editable,
  onLocateSource,
  onLocateTranslation,
  onAction,
  segments,
  selectedFieldId,
}: {
  view: ReviewView;
  file: ReviewFileNode;
  editable: boolean;
  onLocateSource: (f: FieldView) => void;
  onLocateTranslation: (f: FieldView) => void;
  onAction: (a: FieldAction) => void;
  segments: TranslationSegmentView[];
  selectedFieldId?: string | null;
}) {
  const preferenceScope = `${view.case_id}:${view.snapshot_id ?? view.run_id}:${file.parse_artifact_id}:fields`;
  const [logicalId, setLogicalId] = useReviewPreference<string | null>(`${preferenceScope}:logical`, null);
  useEffect(() => { if (selectedFieldId) setLogicalId(null); }, [selectedFieldId, setLogicalId]);
  const fields = useFields(view, logicalId, undefined, file.source.document_id);
  const items = useMemo(() => fields.items.filter((f) => f.document_id === file.source.document_id), [fields.items, file]);
  const logicalOptions = file.logical_documents;

  // 有疑点、有人工处置的组默认展开；其余收起
  const [openGroups, setOpenGroups] = useReviewPreference<Record<string, boolean> | null>(`${preferenceScope}:groups`, null);
  const effectiveOpen = useMemo(() => {
    if (openGroups) return openGroups;
    const initial: Record<string, boolean> = {};
    GROUPS.forEach((g) => {
      const inGroup = items.filter((f) => groupOf(f) === g.key);
      initial[g.key] = inGroup.some((f) => f.issues?.some(issue => !issue.resolved) || f.review_status === "revised" || f.review_status === "unknown" || f.review_status === "candidate");
    });
    if (!Object.values(initial).some(Boolean) && items.length) initial[GROUPS[0].key] = true;
    return initial;
  }, [openGroups, items]);

  const selectedGroup = items.find(field => field.field_id === selectedFieldId);
  const selectedGroupKey = selectedGroup ? groupOf(selectedGroup) : null;
  useEffect(() => {
    if (selectedFieldId && !items.some(f => f.field_id === selectedFieldId) && fields.hasNextPage && !fields.isFetching && !fields.error) void fields.fetchNextPage();
  }, [selectedFieldId, items, fields.hasNextPage, fields.isFetching, fields.error, fields.fetchNextPage]);
  useEffect(() => {
    if (!selectedGroupKey) return;
    setOpenGroups(previous => previous?.[selectedGroupKey] ? previous : { ...previous, [selectedGroupKey]: true });
  }, [selectedGroupKey, setOpenGroups]);
  if (fields.isPending) return <div className="p-6"><Loading text="正在读取字段" /></div>;
  if (!items.length) {
    return <EmptyState icon="doc" title="这份原件还没有抽取到字段" description="字段会在解析与抽取完成后出现；未提取的字段不会以空值伪装成已核对。" />;
  }

  return (
    <div role="region" aria-label="单据字段" tabIndex={0} className="min-h-0 flex-1 overflow-auto overscroll-contain scroll-thin" style={{ containerType: "inline-size", containerName: "review-fields" }}>
      <FileInfoCard file={file} view={view} logicalId={logicalId} onLogical={setLogicalId} options={logicalOptions} />
      {GROUPS.map((g) => {
        const inGroup = items.filter((f) => groupOf(f) === g.key);
        if (!inGroup.length) return null;
        const issues = new Set(inGroup.flatMap(f => (f.issues ?? []).filter(i => !i.resolved).map(i => i.issue_id))).size;
        const open = effectiveOpen[g.key];
        return (
          <section key={g.key} className="border-b border-line">
            <button
              type="button"
              className="flex w-full items-center gap-2 px-4 py-2.5 text-left hover:bg-page"
              aria-expanded={open}
              onClick={() => setOpenGroups({ ...effectiveOpen, [g.key]: !open })}
            >
              <Icon name={open ? "chevron-down" : "chevron-right"} size={14} className="text-ink-3" />
              <span className="text-[13px] font-medium text-ink">{g.label}</span>
              <span className="num text-[12px] text-ink-3">{inGroup.length} 项</span>
              {issues > 0 && <StatusPill tone="risk" icon="alert">{issues} 项有疑点</StatusPill>}
            </button>
            {open && (
              <div>
                {inGroup.map((f) => (
                  <FieldRow
                    key={f.field_id}
                    field={f}
                    editable={editable}
                    allowedActions={view.allowed_actions}
                    selected={selectedFieldId === f.field_id}
                    translation={fieldTranslation(f, segments)}
                    onLocateSource={() => onLocateSource(f)}
                    onLocateTranslation={() => onLocateTranslation(f)}
                    onAction={(kind) => onAction({ field: f, kind })}
                  />
                ))}
              </div>
            )}
          </section>
        );
      })}
      {fields.hasNextPage && (
        <div className="p-3 text-center">
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void fields.fetchNextPage()} disabled={fields.isFetchingNextPage}>
            {fields.isFetchingNextPage ? "正在加载" : "加载更多字段"}
          </button>
        </div>
      )}
      <LineItems view={view} file={file} logicalId={logicalId} />
    </div>
  );
}

function FileInfoCard({ file, view, logicalId, onLogical, options }: { file: ReviewFileNode; view: ReviewView; logicalId: string | null; onLogical: (v: string | null) => void; options: ReviewFileNode["logical_documents"] }) {
  return (
    <div className="border-b border-line bg-page px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-ink-2">
        <span className="max-w-full truncate text-[13px] font-medium text-ink">{file.source.filename}</span>
        <span>原件 v<span className="num">{file.source.document_version}</span></span>
        <span>解析 {file.parse_artifact_id ? shortId(file.parse_artifact_id) : "—"}</span>
        <span>业务版 <span className="num">{view.business_revision_no ?? "—"}</span></span>
        <span>{file.page_count ? `${file.page_count} 页` : file.source.mime.includes("word") ? "段落／表格" : "页数未知"}</span>
        <span>原语言 {file.source.detected_language === "mixed" ? "混排" : languageName(file.source.detected_language)}</span>
        {file.source.language_uncertain && <StatusPill tone="warn" icon="warn">源语言待确认</StatusPill>}
      </div>
      {options.length > 1 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-[12px] text-ink-3">逻辑单据</span>
          <button type="button" className={`btn btn-sm ${logicalId === null ? "btn-secondary" : "btn-ghost"}`} onClick={() => onLogical(null)}>
            全部
          </button>
          {options.map((l) => (
            <button key={l.logical_document_id} type="button" className={`btn btn-sm ${logicalId === l.logical_document_id ? "btn-secondary" : "btn-ghost"}`} onClick={() => onLogical(l.logical_document_id)}>
              {l.label}
              {l.needs_confirmation && <Icon name="warn" size={12} className="text-warn" />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function FieldRow({
  field,
  editable,
  allowedActions,
  selected,
  translation,
  onLocateSource,
  onLocateTranslation,
  onAction,
}: {
  field: FieldView;
  editable: boolean;
  allowedActions: ReviewView["allowed_actions"];
  selected: boolean;
  translation?: TranslationSegmentView;
  onLocateSource: () => void;
  onLocateTranslation: () => void;
  onAction: (kind: FieldAction["kind"]) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const moreRef = useRef<HTMLButtonElement>(null);
  const st = STATUS_TEXT[field.review_status];
  const rowRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (selected) rowRef.current?.scrollIntoView({ block: "nearest" }); }, [selected]);
  const confirmBlocked = !allowedActions.includes("confirm_field") ? "当前状态或账号不允许确认字段" : field.confirm_blocked_reason;
  const pendingIssues = (field.issues ?? []).filter(issue => !issue.resolved);
  useInterfaceLocale();
  const displayName = fieldDisplayName(field);

  return (
    <div ref={rowRef} className={`review-field-row grid min-h-[56px] grid-cols-[minmax(120px,1.1fr)_minmax(140px,1.6fr)_minmax(110px,1fr)_auto] items-start gap-3 border-t border-line px-4 py-2.5 first:border-t-0 ${selected ? "bg-sel-soft" : "hover:bg-page"}`}>
      <div className="min-w-0">
        <p className="text-[13px] font-medium text-ink">{displayName}</p>
        {field.role && !PARTY_ROLES[field.role.toLowerCase().replace(/[\s-]+/g, "_")] && <p className="text-[11px] text-ink-3">原件标注角色：{field.role}</p>}
      </div>
      <div className="min-w-0 text-[13px]">
        <p className="break-words-all text-ink-2" title={field.original_text}>
          <span className="text-[11px] text-ink-3">原文 </span>
          {field.original_text || "未提取"}
        </p>
        <p className="num mt-0.5 break-words-all text-ink">
          <span className="text-[11px] text-ink-3">规范值 </span>
          {field.normalized_value || "未提取"}
          {field.unit ? ` ${field.unit}` : ""}
        </p>
        {field.adopted_value && (
          <p className="mt-0.5 break-words-all text-brand-ink">
            <span className="text-[11px] text-ink-3">复核后采用 </span>
            {field.adopted_value}
          </p>
        )}
        {translation && (
          <p className="mt-0.5 break-words-all text-ink-2">
            <span className="text-[11px] text-ink-3">译文 </span>
            {translation.translated_text}
          </p>
        )}
      </div>
      <div className="min-w-0">
        <StatusPill tone={st.tone} icon={st.icon}>
          {st.text}
        </StatusPill>
        {!!pendingIssues.length && <details className="mt-1 text-[11px] text-ink-2">
          <summary>{pendingIssues.length} 项关联疑点待处置</summary>
          {pendingIssues.map(issue => <p key={issue.issue_id} className={`mt-1 ${issue.severity === "blocking" ? "text-risk" : "text-warn"}`}>{issue.title} · {issue.severity === "blocking" ? "红项" : issue.severity === "warning" ? "黄项" : "无法判断"}</p>)}
        </details>}
        {field.confidence_note && <p className="mt-1 text-[11px] text-ink-3">{field.confidence_note}</p>}
        {field.history?.length ? (
          <div className="mt-1 text-[11px] text-ink-3">
            <details><summary>审核历史（{field.history.length}条）· {formatDateTime(field.history[field.history.length - 1].at)}</summary>{field.history.map((event, index) => <div key={index} className="mt-1 border-l border-line pl-2"><p>{event.by} · {formatDateTime(event.at)} · {event.action}</p>{event.value && <p>采用值：{event.value}</p>}<p>依据：{event.reason || "未填写"}</p></div>)}</details>
          </div>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onLocateSource}>
          定位原文
        </button>
        {translation && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={onLocateTranslation}>
            定位译文
          </button>
        )}
        {editable && (
          <>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => onAction("confirm")} disabled={!!confirmBlocked} title={confirmBlocked ?? "记录本人已核对该字段"}>
              确认无误
            </button>
            <button ref={moreRef} type="button" className="btn btn-ghost btn-sm w-8 px-0" onClick={() => setMenuOpen(true)} aria-label={`${field.name} 更多操作`}>
              <Icon name="more" size={16} />
            </button>
            <Menu
              open={menuOpen}
              onClose={() => setMenuOpen(false)}
              anchor={moreRef}
              label={`${field.name} 操作`}
              items={[
                { label: "修订识别值", icon: "pencil", onSelect: () => onAction("revise"), disabled: !allowedActions.includes("revise_field"), disabledReason: "当前状态或账号不允许修订字段" },
                { label: "标记无法判断", icon: "question", onSelect: () => onAction("mark-unknown"), disabled: !allowedActions.includes("mark_field_unknown"), disabledReason: "当前状态或账号不允许变更字段状态" },
              ]}
            />
          </>
        )}
      </div>
      {confirmBlocked && editable && <p className="col-span-4 -mt-1 text-[11px] text-ink-3">{confirmBlocked}</p>}
    </div>
  );
}

function LineItems({ view, file, logicalId }: { view: ReviewView; file: ReviewFileNode; logicalId: string | null }) {
  const q = useLineItems(view, logicalId);
  const rows = q.items.filter((r) => r.target.document_id === file.source.document_id);
  const [open, setOpen] = useState(true);
  const [matching,setMatching]=useState(false);
  if (!rows.length && !q.hasNextPage) return null;
  return (
    <section className="border-b border-line">
      <button type="button" className="flex w-full items-center gap-2 px-4 py-2.5 text-left hover:bg-page" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <Icon name={open ? "chevron-down" : "chevron-right"} size={14} className="text-ink-3" />
        <span className="text-[13px] font-medium text-ink">货物明细</span>
        <span className="num text-[12px] text-ink-3">{rows.length} 行</span>
      </button>
      {open && (
        <div className="overflow-x-auto scroll-thin px-4 pb-3">
          <div className="mb-2 flex items-center gap-2"><button type="button" className="btn btn-secondary btn-sm" disabled={view.mode!=="working"||!view.run_id||!view.allowed_actions.includes("revise_field")} onClick={()=>setMatching(true)}>确认行关系</button><span className="text-[11px] text-ink-3">人工确认对应关系；复杂关系不自动核对数值</span></div>
          <table className="w-full min-w-[560px] text-table">
            <thead>
              <tr className="border-b border-line text-left text-[12px] text-ink-3">
                <th className="py-1.5 pr-3 font-normal">层级／行号</th>
                <th className="py-1.5 pr-3 font-normal">货描</th>
                <th className="py-1.5 pr-3 font-normal">HS原值</th>
                <th className="py-1.5 pr-3 text-right font-normal">数量</th>
                <th className="py-1.5 pr-3 text-right font-normal">金额</th>
                <th className="py-1.5 font-normal">匹配</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <LineRow key={r.line_id} row={r} />
              ))}
            </tbody>
          </table>
          {q.hasNextPage && <button type="button" className="btn btn-secondary mt-3" disabled={q.isFetchingNextPage} onClick={()=>void q.fetchNextPage()}>加载更多明细</button>}
        </div>
      )}
      {matching&&<LineMatchForm view={view} onClose={()=>setMatching(false)}/>}
    </section>
  );
}

function LineMatchForm({view,onClose}:{view:ReviewView;onClose:()=>void}) {
  const candidates=useLineItems(view,null);const cmd=useReviewCommand((body:Record<string,unknown>,key:string)=>reviewApi.confirmLineMatch(view.case_id,body,key));const toast=useToast();
  const [left,setLeft]=useState<string[]>([]);const [right,setRight]=useState<string[]>([]);const [relation,setRelation]=useState("one_to_one");const [scope,setScope]=useState("");const [level,setLevel]=useState("货品明细");
  const [same,setSame]=useState(false);const [complete,setComplete]=useState(false);const [reason,setReason]=useState("");const [error,setError]=useState<string|null>(null);
  const options=candidates.items;const label=(row:LineItemView)=>`${view.files.find(f=>f.source.document_id===row.target.document_id)?.source.filename??"原件"} · ${row.description} · ${row.quantity} ${row.unit}`;
  const shape=relation==="one_to_one"?left.length===1&&right.length===1:relation==="one_to_many"?left.length===1&&right.length>1:left.length>1&&right.length===1;
  const save=async()=>{setError(null);try{await cmd.mutateAsync({run_id:view.run_id,expected_business_revision_id:view.business_revision_id,expected_review_state_version:view.review_state_version,left_line_ids:left,right_line_ids:right,relation,basis:{same_goods:same,complete,scope_key:scope.trim(),role:"goods",packaging_level:level.trim()},reason:reason.trim()});toast({tone:"ok",text:"行关系已保存，相关规则和预览等待重算"});onClose();}catch(exc){setError(errorText(exc));}};
  return <Dialog open title="确认跨单据行关系" onClose={onClose} width={640} footer={<><button type="button" className="btn btn-secondary" onClick={onClose}>取消</button><button type="button" className="btn btn-primary" disabled={cmd.isPending||!shape||left.some(id=>right.includes(id))||!same||!complete||!scope.trim()||!level.trim()||!reason.trim()} onClick={()=>void save()}>保存关系</button></>}><div className="space-y-3 p-5 text-[13px]">
    <p className="text-ink-2">记录人工核验的行对应关系。一对多／多对一只保存人工依据，数量和重量仍待人工裁定，不自动判通过。原件和历史快照保留。</p>
    <label className="block">关系类型<select className="input mt-1 w-full" value={relation} onChange={e=>{setRelation(e.target.value);setLeft([]);setRight([]);}}><option value="one_to_one">一对一</option><option value="one_to_many">一对多（拆分）</option><option value="many_to_one">多对一（合并）</option></select></label>
    {(["左侧明细","右侧明细"] as const).map((text,i)=>{const multiple=i?relation==="one_to_many":relation==="many_to_one";return <label key={text} className="block">{text}{multiple&&"（可多选，最多20行）"}<select className="input mt-1 w-full" multiple={multiple} size={multiple?Math.min(5,Math.max(2,options.length)):undefined} value={multiple?(i?right:left):(i?right:left)[0]??""} onChange={e=>(i?setRight:setLeft)(Array.from(e.target.selectedOptions,option=>option.value).filter(Boolean).slice(0,20))}>{!multiple&&<option value="">请选择明细</option>}{options.map(row=><option key={row.line_id} value={row.line_id} disabled={(i?left:right).includes(row.line_id)}>{label(row)}</option>)}</select></label>;})}
    {candidates.hasNextPage&&<button type="button" className="btn btn-secondary btn-sm" disabled={candidates.isFetchingNextPage} onClick={()=>void candidates.fetchNextPage()}>加载更多候选明细</button>}
    <label className="block">运输批次／比较范围<input className="input mt-1 w-full" value={scope} onChange={e=>setScope(e.target.value)} placeholder="填写已核验的批次或运输参考"/></label>
    <label className="block">包装层级<input className="input mt-1 w-full" value={level} onChange={e=>setLevel(e.target.value)}/></label>
    <label className="flex gap-2"><input type="checkbox" checked={same} onChange={e=>setSame(e.target.checked)}/>已核对为同一货品</label><label className="flex gap-2"><input type="checkbox" checked={complete} onChange={e=>setComplete(e.target.checked)}/>左右所选行各自合计覆盖同一完整范围</label>
    <label className="block">确认理由与依据<textarea className="input mt-1 w-full" value={reason} onChange={e=>setReason(e.target.value)} rows={3}/></label>
    {error&&<p className="text-risk">{error}；你的填写已保留，请重新加载确认版本后再保存。</p>}
  </div></Dialog>;
}

function LineRow({ row }: { row: LineItemView }) {
  const openEvidence = useSession(state => state.openEvidence);
  const match = row.match_status === "matched" ? { text: "已匹配", tone: "ok" as const, icon: "check" as const } : row.match_status === "partial" ? { text: "部分匹配", tone: "warn" as const, icon: "warn" as const } : { text: "未匹配", tone: "info" as const, icon: "question" as const };
  return (
    <tr className="border-b border-[#eceef1] align-top">
      <td className="sticky left-0 bg-card py-2 pr-3 text-ink-2">{row.line_no ?? "—"} · {row.level}</td>
      <td className="py-2 pr-3 text-ink">{row.description}</td>
      <td className="num py-2 pr-3 text-ink-2" title="原件编码；归类未评估">{row.hs_code ?? "未提取"}</td>
      <td className="num py-2 pr-3 text-right text-ink">
        {row.quantity} {row.unit}
      </td>
      <td className="num py-2 pr-3 text-right text-ink">{row.amount ?? "—"} {row.currency}<div className="text-[11px] text-ink-3">单价 {row.unit_price ?? "未提取"}</div></td>
      <td className="py-2">
        <StatusPill tone={match.tone} icon={match.icon}>
          {match.text}
        </StatusPill>
        <button className="btn btn-ghost btn-sm" onClick={() => openEvidence(row.target, "view_source")}>定位原文</button>
        {row.matches?.map((relation, index) => <details key={index} className="mt-1 text-[11px] text-ink-2"><summary>{relation.relation ?? "对应行"} · {relation.state}</summary><p>匹配依据：{relation.basis ?? "未提供"}</p><p>确认人：{relation.by ?? "未确认"} · {relation.at ? formatDateTime(relation.at) : ""}</p><div className="flex flex-col gap-1">{([relation.left_detail, relation.right_detail]).map((side,i) => side ? <button key={i} className="btn btn-link text-left whitespace-normal" onClick={() => openEvidence(side.target, "view_source")}>{i ? "右侧" : "左侧"}：{side.label}</button> : <span key={i}>关联行未能读取，请重新加载</span>)}</div></details>)}
      </td>
    </tr>
  );
}
