import { useState } from "react";
import { BarChart2, FileText } from "lucide-react";
import { BatchTestDialog } from "./BatchTestDialog";
import { BatchTestProgressDialog } from "./BatchTestProgressDialog";
import { BatchTestReportDialog } from "./BatchTestReportDialog";

export function ReleaseContent() {
  const [showBatchDialog, setShowBatchDialog] = useState(false);
  const [showProgressDialog, setShowProgressDialog] = useState(false);
  const [showReportDialog, setShowReportDialog] = useState(false);
  const [currentBatchId, setCurrentBatchId] = useState("");

  const handleBatchStart = (batchId: string) => {
    setCurrentBatchId(batchId);
    setShowProgressDialog(true);
  };

  const handleBatchComplete = () => {
    setShowReportDialog(true);
  };

  return (
    <main className="relative flex-1 min-w-0 overflow-hidden bg-transparent px-8 py-8 xl:px-10 flex flex-col">
      <div className="absolute inset-0 bg-[#fcfdff]" />
      <div
        className="absolute inset-0 bg-[linear-gradient(rgba(15,23,42,0.028)_1px,transparent_1px),linear-gradient(90deg,rgba(15,23,42,0.024)_1px,transparent_1px)] bg-[size:34px_34px]"
        style={{
          maskImage: "radial-gradient(circle at center, rgba(0,0,0,0.92) 0%, rgba(0,0,0,0.78) 36%, rgba(0,0,0,0.36) 68%, transparent 100%)",
          WebkitMaskImage:
            "radial-gradient(circle at center, rgba(0,0,0,0.92) 0%, rgba(0,0,0,0.78) 36%, rgba(0,0,0,0.36) 68%, transparent 100%)",
        }}
      />

      <div className="relative z-10 flex justify-between items-end border-b border-gray-200/70 pb-6 mb-6 shrink-0">
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <FileText className="w-5 h-5 text-blue-600" />
            <h1 className="app-wordmark-heading text-[20px]">发稿</h1>
          </div>
          <span className="text-[12px] font-medium text-gray-500 tracking-wide">管理品牌发稿能力与后续内容发布流程</span>
        </div>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => setShowBatchDialog(true)}
            className="group inline-flex items-center gap-1.5 px-0 py-2 text-[12px] font-bold text-gray-900 transition-colors hover:text-black"
          >
            <BarChart2 className="w-4 h-4 shrink-0" strokeWidth={2.25} />
            品牌分析
          </button>
        </div>
      </div>

      <BatchTestDialog
        open={showBatchDialog}
        onOpenChange={setShowBatchDialog}
        onStart={handleBatchStart}
      />

      <BatchTestProgressDialog
        open={showProgressDialog}
        onOpenChange={setShowProgressDialog}
        batchId={currentBatchId}
        onComplete={handleBatchComplete}
      />

      <BatchTestReportDialog
        open={showReportDialog}
        onOpenChange={setShowReportDialog}
        batchId={currentBatchId}
      />

      <div className="relative z-10 flex-1 flex items-center justify-center px-8">
        <div className="flex flex-col items-center justify-center gap-1 text-center">
          <p
            className="text-[25px] font-normal tracking-[-0.045em] text-[#b7bbc6]"
            style={{ fontFamily: "'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif" }}
          >
            Please wait for the next version update
            <span className="text-[#14C7F3]">.</span>
          </p>
          <p
            className="text-[13px] leading-[1.1] font-normal tracking-[-0.022em] text-[#aeb4c0]"
            style={{ fontFamily: "'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Georgia, serif" }}
          >
            <span className="opacity-90">Current version:</span>{" "}
            <span className="text-[#9aa1af]">V26.04.12</span>
          </p>
        </div>
      </div>
    </main>
  );
}
