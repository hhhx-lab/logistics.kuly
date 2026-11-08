import ts from 'typescript';
const chinese=s=>/[\u4e00-\u9fff]/.test(s);
const textProps=new Set(['title','label','aria-label','aria-description','description','placeholder','alt','hint','text','closeLabel']);
export function uiLocalizationPlugin(){return {name:'static-ui-localization',enforce:'pre',transform(code,id){
 if(!id.endsWith('.tsx')||id.includes('/i18n/'))return;
 const source=ts.createSourceFile(id,code,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);const edits=[];
 const stringish=e=>ts.isStringLiteral(e)&&chinese(e.text)||ts.isConditionalExpression(e)&&(stringish(e.whenTrue)||stringish(e.whenFalse))||ts.isBinaryExpression(e)&&[ts.SyntaxKind.AmpersandAmpersandToken,ts.SyntaxKind.BarBarToken,ts.SyntaxKind.QuestionQuestionToken].includes(e.operatorToken.kind)&&stringish(e.right)||ts.isTemplateExpression(e)&&chinese(e.getText(source))&&!/filename|source_text|candidate_value|canonical|paragraph|message\.text|translated_text|content|excerpt/.test(e.getText(source));
 const e=(start,end,text)=>edits.push({start,end,text});
 const material=/filename|source_text|candidate_value|canonical|paragraph|message\.text|translated_text|content|excerpt|display_name|instruction|\.name\b|\.value\b|row\.title|row\.description|event\.value|issue\.title|logical\.title|section\.title|ld\.label|s\.title|side\.label/;
 const safeValue=n=>{
  const value=n.getText(source);
  if(/Sidebar|ArchivedConversations/.test(id)&&/c\.title|conv\.title/.test(value))return false;
  if(/FileTree/.test(id)&&/s\.title|ld\.label/.test(value))return false;
  if(/SkillEditor|ExtensionsPage|McpWizard/.test(id)&&/p\.description|t\.description/.test(value))return false;
  if(/MessageList|Sidebar/.test(id)&&/r\.label/.test(value))return false;
  const uiValue=/^(?:title|description|label|modeLabel|stateLabel|placeholder|closeLabel|draftLabel|hint|confirmReason|confirmBlocked|exportReason|cancelBlockedReason|baseReason)$|\.(?:label|group|status_label|version_label|case_status_label|source_label|transport_label|page_range|gate_summary|disabledReason|disabled_reason)$|^(?:st|t|tone|map|qa|cat|match|sev|sv)\.text$|^[A-Z_]+\[.+\]$|^(?:formatTime|formatDateTime|languageName|phaseLabel|runTone)\(/.test(value);
  return uiValue&&!material.test(value);
 };
 function scan(node){
  if(ts.isJsxElement(node)&&node.openingElement.tagName.getText(source)==='UiText')return;
  if(ts.isJsxText(node)&&chinese(node.text)) {const value=node.text.replace(/\s+/g,' ').trim();if(value)e(node.pos,node.end,'<UiText value={'+JSON.stringify(value)+'} />');return;}
  if(ts.isJsxExpression(node)&&node.expression&&!ts.isJsxAttribute(node.parent)) {
   const expression=node.expression.getText(source);
   if(/^(?:errorText\(|rowError$|error$|\w+\.error\.message$)/.test(expression)){e(node.pos,node.end,'{<span lang="en">{englishError('+expression+')}</span>}');return;}
   if(stringish(node.expression)||safeValue(node.expression)){e(node.pos,node.end,'{<UiText value={'+expression+'} />}');return;}
  }
  if(ts.isJsxOpeningElement(node)||ts.isJsxSelfClosingElement(node)){
   const attrs=node.attributes.properties.filter(p=>ts.isJsxAttribute(p)&&textProps.has(p.name.getText(source))&&p.initializer&&(ts.isStringLiteral(p.initializer)&&chinese(p.initializer.text)||ts.isJsxExpression(p.initializer)&&p.initializer.expression&&!/errorText\(/.test(p.initializer.expression.getText(source))&&(stringish(p.initializer.expression)||safeValue(p.initializer.expression))));
   if(attrs.length){
    const tag=node.tagName.getText(source),as=/^[a-z]/.test(tag)?JSON.stringify(tag):'{'+tag+'}';e(node.tagName.pos,node.tagName.end,'UiElement');e(node.attributes.pos,node.attributes.pos,' as='+as+' translateProps={'+JSON.stringify(attrs.map(p=>p.name.getText(source)))+'}');
    if(ts.isJsxOpeningElement(node)){const parent=node.parent;if(ts.isJsxElement(parent))e(parent.closingElement.tagName.pos,parent.closingElement.tagName.end,'UiElement');}
   }
  }
  ts.forEachChild(node,scan);
 }
 scan(source);
 if(!edits.length)return;
 let output=code;for(const edit of edits.sort((a,b)=>b.start-a.start||b.end-a.end))output=output.slice(0,edit.start)+edit.text+output.slice(edit.end);
 const imported=new Set();for(const node of source.statements)if(ts.isImportDeclaration(node)&&ts.isStringLiteral(node.moduleSpecifier)&&node.moduleSpecifier.text.endsWith('/i18n/runtime')&&node.importClause?.namedBindings&&ts.isNamedImports(node.importClause.namedBindings))for(const item of node.importClause.namedBindings.elements)imported.add(item.name.text);
 const missing=['UiText','UiElement',...(output.includes('englishError(')?['englishError']:[])].filter(name=>!imported.has(name));
 return {code:(missing.length?'import {'+missing.join(',')+'} from "/src/i18n/runtime.tsx";\n':'')+output,map:null};
}};}
