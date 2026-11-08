import { useInterfaceLocale, translateUi } from "../../i18n/runtime";
export function useInterfaceText() {
 const language=useInterfaceLocale();
 return (chinese:string,english:string)=>language==="en"?english:translateUi(chinese,language);
}
