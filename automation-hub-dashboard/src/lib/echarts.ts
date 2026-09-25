/**
 * The ECharts build the dashboard uses: only the series and components its
 * charts actually render, instead of the whole library. The full import was
 * about 1.1 MB of JavaScript on the first screen; this is a fraction of that.
 *
 * Adding a chart that needs another series type or component? Register it
 * here, or ECharts will skip that part of the option (it warns in the console).
 */
import * as echarts from "echarts/core";
import { BarChart, CandlestickChart, CustomChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import {
  AxisPointerComponent, DataZoomComponent, GraphicComponent, GridComponent, LegendComponent,
  MarkAreaComponent, MarkLineComponent, MarkPointComponent, TitleComponent, TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  LineChart, BarChart, PieChart, CandlestickChart, ScatterChart, CustomChart,
  GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AxisPointerComponent,
  MarkLineComponent, MarkPointComponent, MarkAreaComponent, GraphicComponent, TitleComponent,
  CanvasRenderer,
]);

export default echarts;
export type { ECharts } from "echarts/core";
