import { ImageWithFallback } from "./figma/ImageWithFallback";

const ARTISTS = [
  { id: 1, name: "Artist Name", genre: "R&B", img: "https://images.unsplash.com/photo-1699427980129-40db19eba9e7?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHxibHVycnklMjBmYWNlJTIwbW90aW9ufGVufDF8fHx8MTc3NDE2NjM2Nnww&ixlib=rb-4.1.0&q=80&w=1080" },
  { id: 2, name: "Artist Name", genre: "Indie pop", img: "https://images.unsplash.com/photo-1730565153790-40d6dca7b2ec?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHxwZXJzb24lMjBseWluZyUyMGRvd24lMjBjb2xvcmZ1bHxlbnwxfHx8fDE3NzQxNjYzNjZ8MA&ixlib=rb-4.1.0&q=80&w=1080" },
  { id: 3, name: "Artist Name", genre: "Hip hop", img: "https://images.unsplash.com/photo-1680108514313-17c095279333?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHx2aW50YWdlJTIwcG9sYXJvaWQlMjBwb3J0cmFpdHxlbnwxfHx8fDE3NzQxNjYzNjZ8MA&ixlib=rb-4.1.0&q=80&w=1080" },
  { id: 4, name: "Artist Name", genre: "Electronic", img: "https://images.unsplash.com/photo-1630890236099-b5dbd8e933ad?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHxnbGl0Y2glMjBhcnQlMjBwb3J0cmFpdHxlbnwxfHx8fDE3NzQxNjYzNjZ8MA&ixlib=rb-4.1.0&q=80&w=1080" },
  { id: 5, name: "Artist Name", genre: "R&B", img: "https://images.unsplash.com/photo-1764698403436-35977fd6e27d?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHxzdHJlZXR3ZWFyJTIwZmFzaGlvbiUyMHBvcnRyYWl0JTIwb3V0ZG9vcnxlbnwxfHx8fDE3NzQxNjYzNjZ8MA&ixlib=rb-4.1.0&q=80&w=1080" },
  { id: 6, name: "Artist Name", genre: "Rock", img: "https://images.unsplash.com/photo-1637066725928-d55765ff664a?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHxtYW4lMjByZWQlMjBiYWNrZ3JvdW5kJTIwcG9ydHJhaXR8ZW58MXx8fHwxNzc0MTY2MzY3fDA&ixlib=rb-4.1.0&q=80&w=1080" },
];

export function ArtistSection() {
  return (
    <section>
      <div className="mb-6">
        <h2 className="text-[28px] font-bold tracking-tight text-gray-900 mb-1">Title</h2>
        <p className="text-gray-500 text-sm">Subheading</p>
      </div>
      
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-5">
        {ARTISTS.map((a) => (
          <div key={a.id} className="group cursor-pointer">
            <div className="relative aspect-square mb-3 overflow-hidden rounded-xl bg-gray-100">
              <ImageWithFallback 
                src={a.img} 
                alt={a.name} 
                className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
              />
            </div>
            <h3 className="font-semibold text-gray-900 text-[15px] truncate tracking-tight">{a.name}</h3>
            <p className="text-[13px] text-gray-500 mt-0.5 truncate">{a.genre}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
