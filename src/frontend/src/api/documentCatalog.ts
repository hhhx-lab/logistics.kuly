import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import { translateUi, useInterfaceLocale } from "../i18n/runtime";

export interface DocumentTypeEntry {
  code: string;
  label: string;
  group: string;
  base_type: string;
  layout: string;
  review_focus: string[];
}
export interface CargoProfileEntry { code: string; label: string; review_focus: string }
export interface DocumentCatalogView { version: string; items: DocumentTypeEntry[]; cargo_profiles: CargoProfileEntry[] }

export function useDocumentCatalog() {
  const language = useInterfaceLocale();
  return useQuery({ queryKey: ["document-catalog"], queryFn: () => api<DocumentCatalogView>("/document-catalog"), staleTime: 300_000, retry: false,
    select: data => ({ ...data, items: data.items.map(item => ({ ...item, label: translateUi(item.label, language), group: translateUi(item.group, language), layout: translateUi(item.layout, language), review_focus: item.review_focus.map(text => translateUi(text, language)) })), cargo_profiles: data.cargo_profiles.map(item => ({ ...item, label: translateUi(item.label, language), review_focus: translateUi(item.review_focus, language) })) }),
  });
}

export function groupedDocumentTypes(items: DocumentTypeEntry[]) {
  return Array.from(new Set(items.map(item => item.group))).map(group => ({ group, items: items.filter(item => item.group === group) }));
}
