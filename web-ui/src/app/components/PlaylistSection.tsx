import { ImageWithFallback } from "./figma/ImageWithFallback";

const PLAYLISTS = [
  { 
    id: 1, 
    title: "Playlist 1", 
    desc: "Description of playlist", 
    img: "https://images.unsplash.com/photo-1692765394287-6ef1fb437594?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHx3b21hbiUyMGxvb2tpbmclMjBhd2F5JTIwcG9ydHJhaXR8ZW58MXx8fHwxNzc0MTY2MzU5fDA&ixlib=rb-4.1.0&q=80&w=1080", 
    topText: "Playlist 1" 
  },
  { 
    id: 2, 
    title: "Playlist 2", 
    desc: "Description of playlist", 
    img: "https://images.unsplash.com/photo-1681152142855-e853058ea075?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHxkYXJrJTIwbW9vZHklMjBwb3J0cmFpdHxlbnwxfHx8fDE3NzQxNjYzNjV8MA&ixlib=rb-4.1.0&q=80&w=1080", 
    topText: "Playlist 2" 
  },
  { 
    id: 3, 
    title: "Playlist 3", 
    desc: "Description of playlist", 
    img: "https://images.unsplash.com/photo-1593882376066-2fef2effd2ed?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHxwZXJzb24lMjBncmVlbiUyMGNsb3RoaW5nJTIwcG9ydHJhaXR8ZW58MXx8fHwxNzc0MTY2MzY1fDA&ixlib=rb-4.1.0&q=80&w=1080", 
    topText: "Playlist 3" 
  },
  { 
    id: 4, 
    title: "Playlist 4", 
    desc: "Description of playlist", 
    img: "https://images.unsplash.com/photo-1561955147-53de1a3099a6?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHxjaXR5JTIwbmlnaHQlMjBsaWdodHMlMjBibHVycnl8ZW58MXx8fHwxNzc0MTY2MzY1fDA&ixlib=rb-4.1.0&q=80&w=1080", 
    topText: "Playlist 4" 
  },
];

export function PlaylistSection() {
  return (
    <section>
      <div className="mb-6">
        <h2 className="text-[28px] font-bold tracking-tight text-gray-900 mb-1">Title</h2>
        <p className="text-gray-500 text-sm">Subheading</p>
      </div>
      
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
        {PLAYLISTS.map((p) => (
          <div key={p.id} className="group cursor-pointer">
            <div className="relative aspect-square mb-4 overflow-hidden rounded-xl bg-gray-100">
              <ImageWithFallback 
                src={p.img} 
                alt={p.title} 
                className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
              />
              <div className="absolute inset-0 bg-gradient-to-b from-black/50 via-transparent to-transparent opacity-90 transition-opacity group-hover:opacity-100" />
              <span className="absolute top-5 left-5 text-white font-bold text-2xl tracking-tight drop-shadow-md">
                {p.topText}
              </span>
            </div>
            <h3 className="font-semibold text-gray-900 text-base truncate tracking-tight">{p.title}</h3>
            <p className="text-[13px] text-gray-500 mt-0.5 truncate">{p.desc}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
