import { createElement, useSyncExternalStore, type ElementType, type ReactNode } from "react";
import en from "./en.json";
import fr from "./fr.json";
import es from "./es.json";
import ru from "./ru.json";
import source from "./source.json";
import type { TargetLanguage } from "../api/types";
const dictionaries: Record<string, Record<string,string>> = {en,fr,es,ru};
const englishSources = new Map(Object.entries(en).map(([key,value])=>[value,key]));
const errorSources = new Map(Object.values(dictionaries).flatMap(dictionary=>Object.entries(dictionary).map(([key,value])=>[value,key] as const)));
const anonymousLanguageKey="logistics.anonymous-interface-language";
export function getAnonymousLocale():TargetLanguage {
 try {const value=localStorage.getItem(anonymousLanguageKey);if(value&&["zh-CN","en","fr","es","ru"].includes(value))return value as TargetLanguage;} catch {}
 return "zh-CN";
}
const currentLanguage=getAnonymousLocale();
const scope=globalThis as typeof globalThis & {__logisticsUiLocale?: {locale:TargetLanguage;listeners:Set<()=>void>}};
const state=scope.__logisticsUiLocale ??= {locale:(["zh-CN","en","fr","es","ru"].includes(currentLanguage)?currentLanguage:"zh-CN") as TargetLanguage,listeners:new Set<()=>void>()};
const escape=(value:string)=>value.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
const templates=source.filter(key=>/\{\d+\}/.test(key)&&/[\u4e00-\u9fff]/.test(key)).sort((a,b)=>b.replace(/\{\d+\}/g,"").length-a.replace(/\{\d+\}/g,"").length).map(key=>({key,pattern:new RegExp("^"+key.split(/\{\d+\}/).map(escape).join("(.*?)")+"$","s")}));
export function getInterfaceLocale(){return state.locale;}
export function setInterfaceLocale(value:TargetLanguage) {document.documentElement.lang=value;document.title=translateUi("物流单证智能体",value);if(value===state.locale)return;state.locale=value;state.listeners.forEach(fn=>fn());}
export function setAnonymousLocale(value:TargetLanguage) {try {localStorage.setItem(anonymousLanguageKey,value);} catch {}setInterfaceLocale(value);}
export function useInterfaceLocale() {return useSyncExternalStore(fn=>{state.listeners.add(fn);return()=>state.listeners.delete(fn);},()=>state.locale,()=>"zh-CN" as TargetLanguage);}
export function translateUi(value:string,language=state.locale):string {
 if(language==="zh-CN")return value;
 const dictionary=dictionaries[language];if(dictionary?.[value])return dictionary[value];
 for(const template of templates){const translated=dictionary?.[template.key];if(!translated)continue;const match=value.match(template.pattern);if(match)return translated.replace(/\{(\d+)\}/g,(_,index)=>match[Number(index)+1]??"");}
 return value;
}
export function englishError(value: string): string {
 const sourceKey=errorSources.get(value);
 if(sourceKey&&en[sourceKey as keyof typeof en])return en[sourceKey as keyof typeof en];
 const translated = translateUi(value, "en");
 return /[\u4e00-\u9fff]/.test(translated) ? "The operation could not be completed. Please retry or reload the latest data." : translated;
}
export function UiText({value}:{value:ReactNode}) {const language=useInterfaceLocale();return typeof value==="string"?translateUi(englishSources.get(value)??value,language):value;}
export function UiElement({as,translateProps,children,...props}:{as:ElementType;translateProps:string[];children?:ReactNode;[key:string]:unknown}) {
 const language=useInterfaceLocale();const translated={...props};for(const key of translateProps)if(typeof translated[key]==="string")translated[key]=translateUi(translated[key] as string,language);
 return createElement(as,translated,children);
}
