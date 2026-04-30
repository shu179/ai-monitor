export function TopBar() {
  return (
    <div className="sticky top-0 z-10 bg-white/90 backdrop-blur-xl px-10 py-6 flex items-center justify-between">
      <div className="flex items-center bg-gray-100/80 p-1 rounded-lg">
        <TabButton label="Tab" active />
        <TabButton label="Tab" />
        <TabButton label="Tab" />
      </div>
      <button className="bg-[#111] hover:bg-black text-white text-[13px] font-semibold px-4 py-2 rounded-lg shadow-sm transition-transform active:scale-95">
        Call to action
      </button>
    </div>
  );
}

function TabButton({ label, active = false }: { label: string; active?: boolean }) {
  return (
    <button 
      className={`px-5 py-1.5 text-[13px] font-semibold rounded-md transition-all duration-200 ${
        active 
          ? "bg-white text-black shadow-[0_1px_3px_rgba(0,0,0,0.1)]" 
          : "text-gray-500 hover:text-gray-900"
      }`}
    >
      {label}
    </button>
  );
}
