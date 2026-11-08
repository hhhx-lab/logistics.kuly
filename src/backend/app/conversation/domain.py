"""发布的通用单证说明；不含单票数值、裁定或实时法规结论。"""
import re

EXPLANATIONS = {
    "提单": "提单（Bill of Lading，B/L）通常是承运人收货及运输合同的证明；可转让提单还具有货物权利凭证的作用。核对时关注托运人、收货人、通知方、装卸港、件数和提单号。海运单与可转让提单的交付安排不同，具体以单证类型及运输约定为准。",
    "商业发票": "商业发票（Commercial Invoice）用于说明交易双方、货物、价格、币种及贸易条件。它可用于贸易结算和申报资料核对；不能仅凭发票判定货物已经通关或其他单据无误。",
    "装箱单": "装箱单（Packing List）说明货物的包装和装运明细，常见内容包括件数、包装种类、唛头、毛重、净重和体积。它侧重实物装箱信息，应与发票和运输单据交叉核对。",
    "毛重": "毛重（Gross Weight）包含货物与包装的重量；净重（Net Weight）通常指货物本身的重量。核对不同单据时应先统一重量单位和对应的货物范围，不能直接比较不同口径的数字。",
    "净重": "净重（Net Weight）通常指货物本身的重量；毛重（Gross Weight）包含货物与包装的重量。核对时应确认单位、包装口径和货物范围。",
    "收货人": "收货人（Consignee）是运输单据中列示的收货主体；通知方（Notify Party）是到货等事项的通知对象，二者可以不同。收货人也不必然等同于商业发票上的买方。",
    "通知方": "通知方（Notify Party）是运输单据中约定接收到货等通知的主体，未必拥有提货权，也未必等于收货人。应按运输单据的具体约定核对。",
    "信用证": "信用证（Letter of Credit，L/C）是银行按规定条件作出的付款承诺。相关业务通常按信用证条款审查单据；单据与条款是否相符需要逐项核对，不能仅凭一般解释判定付款或放行。",
    "原产地证": "原产地证（Certificate of Origin）证明货物的原产地信息。不同贸易安排可能使用不同证书及要求；当前解释不能替代适用安排下的资格和申报审查。",
    "FOB": "FOB（Free on Board，船上交货）是适用于海运或内河运输的贸易术语。在约定装运港，货物装上买方指定船舶时通常完成交付并转移风险。具体费用和义务应按合同引用的 Incoterms 版本及约定核对。",
    "CIF": "CIF（Cost, Insurance and Freight，成本、保险费加运费）适用于海运或内河运输。卖方安排到约定目的港的运费和保险，但风险通常在装运港货物装船时转移。风险转移与费用承担的地点不同，应按合同引用版本核对。",
    "HS编码": "HS 编码用于商品分类。具体归类取决于货物的材料、用途、组成和适用规则，不能只按商品名称确定；本系统的一般说明不构成正式归类或通关结论。",
}
ALIASES = {"bill of lading": "提单", "b/l": "提单", "commercial invoice": "商业发票", "发票": "商业发票", "packing list": "装箱单", "gross weight": "毛重", "net weight": "净重", "consignee": "收货人", "notify party": "通知方", "letter of credit": "信用证", "hs code": "HS编码", "hs 编码": "HS编码"}


def is_instruction_attack(question: str) -> bool:
    from app.conversation.safety import requests_internal_information
    return requests_internal_information(question) or bool(re.search(r"(忽略|无视|绕过).{0,16}(系统|提示词|规则|权限)|泄露.{0,12}(密钥|提示词|token)|系统提示词|ignore.{0,30}(previous|system).{0,20}(instruction|prompt)|reveal.{0,20}(secret|system prompt)|jailbreak", question, re.I))


def is_greeting(question: str) -> bool:
    return bool(re.fullmatch(r"(?:你好|您好|嗨|哈喽|早上好|下午好|晚上好|hello|hi|hey)(?:[，,\s]*(?:助手|单证助手))?[!！。.,，?？\s]*", question.strip(), re.I))


def asks_case_specific(question: str) -> bool:
    return bool(re.search(r"这(?:票|份|张|批|单|个文件)|本(?:票|单|文件|批次)|该(?:票|份|单|文件)|我(?:的|们的)|上述|前述|附件|其中|里面|已上传|刚上传|已关联.{0,8}(?:单票|单据|文件|材料)|当前(?:单票|单据|文件)|根据.{0,8}(单据|文件)|\.(?:pdf|docx|png|jpe?g)(?![a-z])|this (?:invoice|shipment|document)|our (?:invoice|shipment)", question, re.I))


def run_progress_request(question: str) -> bool:
    return bool(re.search(r"进度|状态|失败|重试|(?:任务|处理|翻译|作业|识别).{0,6}到哪|^(?:现在|目前)?到哪(?:了|一步)?[？?。\s]*$",question))


def general_knowledge_request(question: str) -> bool:
    if is_instruction_attack(question) or asks_case_specific(question):
        return False
    return bool(re.search(r"物流|运输|提单|发票|单证|装箱单|交单|信用证|贸易|incoterms?|FOB|CIF|运费|海运|货代|报关|结算|packing|bill of lading", question, re.I)
        and re.search(r"为什么|为何|如何|怎么理解|区别|含义|解释|介绍|一般|通常|what|why|explain|difference|how", question, re.I))


def translation_task_request(question: str) -> bool:
    question = re.sub(r"(?:不要|不用|无需|不需要|不必|不).{0,3}翻译|(?:do not|don't)\s+translate|no\s+translation", "", question, flags=re.I)
    if not re.search(r"翻译|译成|translate", question, re.I):
        return False
    if not re.search(r"^(?:请)?(?:帮我|替我|把|将).{0,40}(?:翻译|译成)|^(?:请)?(?:翻译|译成)(?:一下|这|附件|文件|单据)|^请.{0,10}进行.{0,5}翻译", question.strip()) and re.search(r"为什么|为何|如何|怎么|有哪些|是什么|什么意思|注意事项|区别|流程|原理|what|why|how", question, re.I):
        return False
    return True


def comparison_task_request(question: str) -> bool:
    # 询问已有核对结果是只读问答；只有明确发起或重算才创建任务。
    positive = re.sub(r"(?:不要|不用|无需|不必|不)(?:启动|发起|创建|重跑|重算).{0,6}(?:任务|核对)", "", question)
    if re.search(r"不(?:要|用|必)?(?:启动|发起|创建|重跑|重算).{0,6}(?:任务|核对)|只(?:读取|查看|解释).{0,12}(?:结果|差异)|(?:核对|比对|对比|比较)(?:的)?结果|(?:为何|为什么|解释).{0,16}(?:差异|冲突)",question,re.I) and not re.search(r"(?:重新|再|发起|启动|执行|创建).{0,8}(?:核对|比对|对比|比较|任务)",positive):
        return False
    return bool(re.search(r"(?:核对|比对|比较|对比|检查).{0,30}(?:同票|这票|这些|发票|箱单|单据|单证)|(?:同票|这票|单证).{0,20}(?:核对|比对|对比)|compare.{0,40}(?:documents|invoice|packing)", question, re.I)
        and not general_knowledge_request(question))


def capability_conflict(question: str, selection: dict) -> str | None:
    workflow = selection.get("workflow_type")
    translation_requested = translation_task_request(question)
    if not workflow:
        if selection.get("allowed_actions") and translation_requested:
            return "所选能力是查询动作，当前要求翻译；请移除或改选翻译能力后继续。"
        return None
    if workflow == "read_index" and translation_requested:
        return "所选能力只负责识别，当前文字要求翻译；请改选翻译能力或确认只读识别。"
    if workflow == "read_index" and general_knowledge_request(question):
        return "所选能力将识别原件，当前问题是一般知识解释；请移除能力后继续提问。"
    if workflow == "consignment_check" and translation_requested:
        return "所选能力只核对同票材料，请改选同票核对与翻译以生成译本。"
    if workflow == "translate_review":
        if re.search(r"(?:不要|不用|无需|不需要).{0,4}翻译|只(?:需|要)?.{0,4}识别", question) or general_knowledge_request(question):
            return "所选能力将发起翻译，但当前问题要求识别或一般知识解释；请移除或改选能力后继续。"
        language = "en" if re.search(r"英文|英语|English", question, re.I) else "zh-CN" if re.search(r"中文|汉语|Chinese", question, re.I) else None
        selected_language = (selection.get("user_parameters") or {}).get("target_language")
        if translation_requested and language and selected_language and language != selected_language:
            return "所选能力参数与本轮要求的目标语言不同；请修改语言参数后继续。"
    return None


def general_explanation(question: str) -> str | None:
    if is_instruction_attack(question) or asks_case_specific(question):
        return None
    text = question.lower()
    if any(word in text for word in ("日期", "期限", "交单", "关系", "如何", "怎么办", "为什么", "应当", "风险")):
        return None
    if not any(x in text for x in ("什么", "解释", "区别", "含义", "意思", "介绍", "怎么理解", "what", "explain", "difference")):
        return None
    found = []
    for alias, term in sorted({**{key: key for key in EXPLANATIONS}, **ALIASES}.items(), key=lambda item: -len(item[0])):
        if alias.lower() in text and term not in found:
            found.append(term)
    if not found:
        return None
    return "以下是通用单证说明，不是对本票内容或通关结果的判定。\n" + "\n\n".join(EXPLANATIONS[term] for term in found[:3])
