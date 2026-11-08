import ts from 'typescript';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const project=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
process.chdir(project);
const root=path.resolve('src');
const catalog=new Set();
const chinese=s=>/[\u4e00-\u9fff]/.test(s);
function files(dir){return fs.readdirSync(dir,{withFileTypes:true}).flatMap(e=>e.isDirectory()?files(path.join(dir,e.name)):[path.join(dir,e.name)]);}
for(const file of files(root).filter(f=>/\.tsx?$/.test(f)&&!f.includes('/i18n/'))){
 const source=ts.createSourceFile(file,fs.readFileSync(file,'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 function scan(node){
  if(ts.isJsxText(node)&&chinese(node.text))catalog.add(node.text.replace(/\s+/g,' ').trim());
  if(ts.isStringLiteral(node)&&chinese(node.text)&&!ts.isImportDeclaration(node.parent)&&!ts.isRegularExpressionLiteral(node))catalog.add(node.text);
  if(ts.isTemplateExpression(node)&&chinese(node.getText(source)))catalog.add(node.head.text+node.templateSpans.map((span,index)=>`{${index}}`+span.literal.text).join(''));
  ts.forEachChild(node,scan);
 }
 scan(source);
}
const existing=JSON.parse(fs.readFileSync('src/i18n/source.json','utf8'));
for(const key of existing)catalog.add(key);
fs.writeFileSync('src/i18n/source.json',JSON.stringify([...catalog].sort(),null,2)+'\n');
console.log('静态UI文案',catalog.size);
