import type { TargetLanguage } from "../api/types";
import { getInterfaceLocale, translateUi } from "./runtime";
export const LANGUAGES: { code: TargetLanguage; label: string; name: string; pattern: RegExp }[] = [
 {code:"zh-CN",label:"中文",name:"中文",pattern:/中文|汉语|chinese|zh-CN/i},
 {code:"en",label:"English",name:"英文",pattern:/英文|英语|english|\ben\b/i},
 {code:"fr",label:"Français",name:"法语",pattern:/法语|法文|french|français|\bfr\b/i},
 {code:"es",label:"Español",name:"西班牙语",pattern:/西班牙语|西语|spanish|español|\bes\b/i},
 {code:"ru",label:"Русский",name:"俄语",pattern:/俄语|俄文|russian|русский|\bru\b/i},
];
export const languageName = (code?: string | null) => translateUi(LANGUAGES.find(item=>item.code===code)?.name ?? code ?? "未识别", getInterfaceLocale());
export const languageNativeName = (code?: string | null) => LANGUAGES.find(item=>item.code===code)?.label ?? code ?? "";
export const languageCode = (code?: string | null): TargetLanguage | undefined => LANGUAGES.find(item=>item.code===code)?.code;
export const languagePreference = (content: string) => {const choices=LANGUAGES.filter(item=>item.pattern.test(content));return choices.length===1?choices[0].code:null;};
