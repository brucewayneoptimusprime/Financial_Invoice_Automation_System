import { downloadFile } from "../download";
import { ExportMenu, type ExportFormat, type ExportLevel } from "./ExportMenu";

// ONE component for exporting one purchase order, used at the far right of every PO row and at the top of a PO page, with
// identical behaviour: level (Financial / Full with metadata) and format (PDF, Word, Excel, CSV). Ticked rows never affect it.

export function poExportPath(poId: number, format: ExportFormat, level: ExportLevel): string {
  return `/api/pos/${poId}/export?${new URLSearchParams({ format, level })}`;
}

export function POExportButton({ poId, poNumber, size = "normal" }: { poId: number; poNumber: string; size?: "small" | "normal" }) {
  return (
    <ExportMenu label="Export" ariaLabel={`Export ${poNumber}`} small={size === "small"} withLevels
                onExport={(format, level) => downloadFile(poExportPath(poId, format, level), `${poNumber}-${level}.${format}`)} />
  );
}
