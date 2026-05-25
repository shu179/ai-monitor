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
  /** Y 轴数字从容器最左 x=0 起左对齐（用于和上方标题/数字对齐），且收窄 YAxis 宽度保持与曲线的自然距离 */
  yLabelAtLeft?: boolean;
  alignEdgeXTicks?: boolean;
};

function LeftAlignedYTick({ y, payload }: { y?: number; payload?: { value: number | string } }) {
  return (
    <text
      x={0}
      y={y}
      fontSize={10}
      fontWeight={400}
      fill="#9ca3af"
      textAnchor="start"
      dominantBaseline="middle"
    >
      {payload?.value}
    </text>
  );
}

function EdgeAlignedXAxisTick({
  x,
  y,
  payload,
  index,
  total,
}: {
  x?: number;
  y?: number;
  payload?: { value: number | string };
  index?: number;
  total: number;
}) {
  const safeIndex = Number(index ?? 0);
  const isFirst = safeIndex === 0;
  const isLast = total > 0 && safeIndex === total - 1;
  const textAnchor = isFirst ? "start" : isLast ? "end" : "middle";

  return (
    <text
      x={x}
      y={(y ?? 0) + 10}
      fontSize={10}
      fontWeight={400}
      fill="#9ca3af"
      textAnchor={textAnchor}
    >
      {payload?.value}
    </text>
  );
}

export function ChartArea({ data, margin, xAxisDy = 10, yLabelAtLeft = false, alignEdgeXTicks = false }: ChartAreaProps) {
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
          tick={alignEdgeXTicks ? <EdgeAlignedXAxisTick total={chartData.length} /> : { fontSize: 10, fill: '#9ca3af', fontWeight: 400 }}
          dy={xAxisDy}
        />
        {yLabelAtLeft ? (
          <YAxis
            key="yaxis"
            axisLine={false}
            tickLine={false}
            ticks={[0, 25, 50, 75, 100]}
            domain={[0, 100]}
            width={22}
            tick={<LeftAlignedYTick />}
          />
        ) : (
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
        )}
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
