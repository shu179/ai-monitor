import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid
} from 'recharts';

type ChartAreaProps = {
  data?: any[];
  margin?: Partial<{ top: number; right: number; left: number; bottom: number }>;
  xAxisDy?: number;
};

export function ChartArea({ data, margin, xAxisDy = 10 }: ChartAreaProps) {
  const defaultData = [
    { name: '周一', value: 30, predict: 40 },
    { name: '周二', value: 45, predict: 42 },
    { name: '周三', value: 35, predict: 50 },
    { name: '周四', value: 65, predict: 55 },
    { name: '周五', value: 85, predict: 70 },
    { name: '周六', value: 65, predict: 80 },
    { name: '周日', value: 95, predict: 85 },
  ];

  const chartData = data || defaultData;
  const chartMargin = {
    top: margin?.top ?? 10,
    right: margin?.right ?? 10,
    left: margin?.left ?? 6,
    bottom: margin?.bottom ?? 20,
  };

  return (
    <div className="w-full h-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart
          data={chartData}
          margin={chartMargin}
      >
        <defs key="defs">
          <linearGradient id="colorValue" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="#2FB8E6" stopOpacity={0.22}/>
            <stop offset="95%" stopColor="#2FB8E6" stopOpacity={0}/>
          </linearGradient>
          <linearGradient id="colorPredict" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="#9ca3af" stopOpacity={0.15}/>
            <stop offset="95%" stopColor="#9ca3af" stopOpacity={0}/>
          </linearGradient>
        </defs>
        <CartesianGrid key="grid" strokeDasharray="3 3" vertical={false} stroke="#f3f4f6" />
        <XAxis 
          key="xaxis"
          dataKey="name" 
          axisLine={false} 
          tickLine={false} 
          tick={{ fontSize: 10, fill: '#9ca3af', fontWeight: 400 }}
          dy={xAxisDy}
        />
        <YAxis 
          key="yaxis"
          axisLine={false} 
          tickLine={false} 
          ticks={[0, 25, 50, 75, 100]}
          domain={[0, 100]}
          width={34}
          tick={{ fontSize: 10, fill: '#9ca3af', fontWeight: 400 }}
          dx={-2}
        />
        <Tooltip 
          key="tooltip"
          contentStyle={{ 
            borderRadius: '8px', 
            border: 'none', 
            boxShadow: '0 4px 12px -2px rgba(0,0,0,0.1)',
            fontSize: '11px',
            fontWeight: '500',
            color: '#111827'
          }} 
        />
        <Area 
          key="area-predict"
          type="monotone" 
          dataKey="predict" 
          name="预测值"
          stroke="#9ca3af" 
          strokeWidth={2}
          strokeDasharray="4 4"
          fillOpacity={1} 
          fill="url(#colorPredict)" 
        />
        <Area 
          key="area-value"
          type="monotone" 
          dataKey="value" 
          name="实际值"
          stroke="#2FB8E6" 
          strokeWidth={2}
          fillOpacity={1} 
          fill="url(#colorValue)" 
        />
      </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
