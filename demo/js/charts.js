/* ============================================
   RCA Pilot — Visualization Helpers
   D3 (fishbone, topology, heatmap, clusters)
   Chart.js (trends, radar)
   ============================================ */

window.RCACharts = (function () {

  // ---- Chart.js global defaults ----
  function setupChartDefaults() {
    if (!window.Chart) return;
    Chart.defaults.font.family = "'Inter', sans-serif";
    Chart.defaults.font.size = 11;
    Chart.defaults.color = '#64748B';
    Chart.defaults.borderColor = '#E5E7EB';
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.legend.labels.boxWidth = 8;
    Chart.defaults.plugins.legend.labels.boxHeight = 8;
    Chart.defaults.plugins.tooltip.backgroundColor = '#0F172A';
    Chart.defaults.plugins.tooltip.titleFont = { weight: '600', size: 12 };
    Chart.defaults.plugins.tooltip.bodyFont = { size: 11 };
    Chart.defaults.plugins.tooltip.padding = 10;
    Chart.defaults.plugins.tooltip.cornerRadius = 8;
    Chart.defaults.plugins.tooltip.boxPadding = 4;
  }

  // Destroy existing chart on a canvas
  function destroyChart(canvas) {
    const existing = Chart.getChart(canvas);
    if (existing) existing.destroy();
  }

  // ============================================================
  //  Chart.js: Deviation trend (30-day stacked bar)
  // ============================================================
  function renderDeviationTrend(canvasId, data) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    destroyChart(canvas);

    const colors = ['#0B5FFF', '#0E7C66', '#F59E0B', '#7C3AED', '#D72638'];
    const datasets = data.depts.map((dept, i) => ({
      label: dept,
      data: data.data[dept],
      backgroundColor: colors[i % colors.length],
      borderRadius: 2,
      borderSkipped: false,
      stack: 'deviations',
    }));

    new Chart(canvas, {
      type: 'bar',
      data: { labels: data.labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { position: 'top', align: 'end', labels: { boxWidth: 8, padding: 12 } },
          tooltip: { callbacks: { footer: (items) => '合计: ' + items.reduce((s, i) => s + i.raw, 0) } },
        },
        scales: {
          x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 15 } },
          y: { stacked: true, grid: { color: '#F1F5F9' }, beginAtZero: true, ticks: { stepSize: 2 } },
        },
      },
    });
  }

  // ============================================================
  //  Chart.js: EM trend line with alert/action lines
  // ============================================================
  function renderEMTrend(canvasId, data) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    destroyChart(canvas);

    const datasets = [
      {
        label: 'Room-3B-Act-12', data: data.act12, borderColor: '#D72638', backgroundColor: 'rgba(215,38,56,0.08)',
        fill: true, tension: 0.35, borderWidth: 2.5, pointRadius: 4, pointBackgroundColor: '#D72638', pointBorderWidth: 2, pointBorderColor: '#fff',
      },
    ];
    if (data.act08 && data.act08.length) {
      datasets.push({
        label: 'Room-3B-Act-08', data: data.act08, borderColor: '#0E7C66', backgroundColor: 'transparent',
        tension: 0.35, borderWidth: 1.5, borderDash: [4, 4], pointRadius: 3, pointBackgroundColor: '#0E7C66',
      });
    }
    if (data.act15 && data.act15.length) {
      datasets.push({
        label: 'Room-3B-Act-15', data: data.act15, borderColor: '#64748B', backgroundColor: 'transparent',
        tension: 0.35, borderWidth: 1.5, borderDash: [4, 4], pointRadius: 3, pointBackgroundColor: '#64748B',
      });
    }

    // Alert & action threshold lines via annotation plugin substitute (custom dataset)
    datasets.push({
      label: '警戒线', data: data.labels.map(() => data.alertLine), borderColor: '#F59E0B', borderWidth: 1, borderDash: [6, 4], pointRadius: 0, fill: false,
    });
    datasets.push({
      label: '行动线', data: data.labels.map(() => data.actionLine), borderColor: '#D72638', borderWidth: 1, borderDash: [6, 4], pointRadius: 0, fill: false,
    });

    new Chart(canvas, {
      type: 'line',
      data: { labels: data.labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { position: 'top', align: 'end', labels: { boxWidth: 8, padding: 10, filter: (item) => !['警戒线', '行动线'].includes(item.text) } },
          tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${ctx.raw} CFU/孔` } },
        },
        scales: {
          x: { grid: { display: false } },
          y: { beginAtZero: true, grid: { color: '#F1F5F9' }, title: { display: true, text: 'CFU/孔', size: 10 } },
        },
      },
    });
  }

  // ============================================================
  //  Chart.js: HVAC pressure trend
  // ============================================================
  function renderHVACTrend(canvasId, data) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    destroyChart(canvas);

    new Chart(canvas, {
      type: 'line',
      data: {
        labels: data.labels,
        datasets: [
          {
            label: 'AHU-7 差压 (Pa)', data: data.values, borderColor: '#0B5FFF', backgroundColor: 'rgba(11,95,255,0.08)',
            fill: true, tension: 0.3, borderWidth: 2.5, pointRadius: 5, pointBackgroundColor: '#0B5FFF', pointBorderColor: '#fff', pointBorderWidth: 2,
          },
          {
            label: '维护阈值', data: data.labels.map(() => data.threshold), borderColor: '#D72638', borderWidth: 1, borderDash: [6, 4], pointRadius: 0, fill: false,
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'top', align: 'end', labels: { boxWidth: 8, filter: (i) => i.text !== '维护阈值' } },
          tooltip: { callbacks: { label: (ctx) => `${ctx.raw} Pa` } },
        },
        scales: {
          x: { grid: { display: false } },
          y: { beginAtZero: false, suggestedMin: 350, suggestedMax: 520, grid: { color: '#F1F5F9' }, title: { display: true, text: 'ΔP (Pa)', size: 10 } },
        },
      },
    });
  }

  // ============================================================
  //  Chart.js: CAPA effectiveness radar
  // ============================================================
  function renderCAPARadar(canvasId, data) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    destroyChart(canvas);

    new Chart(canvas, {
      type: 'radar',
      data: {
        labels: data.labels,
        datasets: [
          { label: '当前效力', data: data.current, borderColor: '#0B5FFF', backgroundColor: 'rgba(11,95,255,0.12)', borderWidth: 2, pointBackgroundColor: '#0B5FFF', pointRadius: 4 },
          { label: '目标', data: data.target, borderColor: '#0E7C66', backgroundColor: 'transparent', borderWidth: 1.5, borderDash: [5, 5], pointRadius: 0 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'top', align: 'end', labels: { boxWidth: 8 } } },
        scales: {
          r: { beginAtZero: true, max: 100, ticks: { stepSize: 25, backdropColor: 'transparent', size: 9 }, grid: { color: '#E5E7EB' }, angleLines: { color: '#E5E7EB' }, pointLabels: { font: { size: 10 } } },
        },
      },
    });
  }

  // ============================================================
  //  D3: Fishbone diagram (6M)
  // ============================================================
  function renderFishbone(containerId, fishboneData, onCauseClick) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = '';

    const width = container.clientWidth || 900;
    const height = 380;
    const margin = { top: 20, right: 120, bottom: 20, left: 60 };

    const svg = d3.select(container).append('svg')
      .attr('class', 'fishbone-svg')
      .attr('width', '100%').attr('height', height)
      .attr('viewBox', `0 0 ${width} ${height}`);

    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    const innerW = width - margin.left - margin.right;
    const innerH = height - margin.top - margin.bottom;

    // Spine (horizontal line to root)
    const spineY = innerH / 2;
    const spineStart = 40;
    const spineEnd = innerW - 140;
    g.append('line').attr('class', 'spine')
      .attr('x1', spineStart).attr('y1', spineY)
      .attr('x2', spineEnd).attr('y2', spineY);

    // Root (effect) box
    const rootG = g.append('g').attr('transform', `translate(${spineEnd}, ${spineY})`);
    rootG.append('rect').attr('class', 'root-node')
      .attr('x', 0).attr('y', -28).attr('width', 130).attr('height', 56).attr('rx', 8);
    rootG.append('text').attr('x', 65).attr('y', -2).attr('text-anchor', 'middle')
      .attr('fill', '#fff').attr('font-weight', '700').attr('font-size', '11px').text('偏差');
    rootG.append('text').attr('x', 65).attr('y', 14).attr('text-anchor', 'middle')
      .attr('fill', '#fff').attr('font-size', '10px')
      .text(fishboneData.root.length > 18 ? fishboneData.root.slice(0, 18) + '…' : fishboneData.root);

    // 6 categories — 3 above, 3 below
    const cats = fishboneData.categories;
    const positions = [
      { x: spineStart + (spineEnd - spineStart) * 0.15, y: spineY, side: 'top' },
      { x: spineStart + (spineEnd - spineStart) * 0.38, y: spineY, side: 'top' },
      { x: spineStart + (spineEnd - spineStart) * 0.61, y: spineY, side: 'top' },
      { x: spineStart + (spineEnd - spineStart) * 0.18, y: spineY, side: 'bottom' },
      { x: spineStart + (spineEnd - spineStart) * 0.41, y: spineY, side: 'bottom' },
      { x: spineStart + (spineEnd - spineStart) * 0.64, y: spineY, side: 'bottom' },
    ];

    cats.forEach((cat, i) => {
      const pos = positions[i];
      const branchLen = 110;
      const dir = pos.side === 'top' ? -1 : 1;
      const bx = pos.x + 50;
      const by = pos.y + dir * branchLen;

      // Branch diagonal line
      g.append('line').attr('class', 'branch-line')
        .attr('x1', pos.x).attr('y1', pos.y)
        .attr('x2', bx).attr('y2', by);

      // Category node
      const catG = g.append('g').attr('transform', `translate(${bx}, ${by})`);
      catG.append('rect').attr('class', 'category-node')
        .attr('x', -48).attr('y', dir < 0 ? -22 : -22).attr('width', 96).attr('height', 24).attr('rx', 12);
      catG.append('text').attr('x', 0).attr('y', dir < 0 ? -6 : -6).attr('text-anchor', 'middle')
        .attr('font-weight', '600').attr('font-size', '10px').attr('fill', '#0B5FFF').text(cat.name);

      // Causes along the branch
      const causeSpacing = 36;
      cat.causes.forEach((cause, j) => {
        const t = (j + 1) / (cat.causes.length + 1);
        const cx = pos.x + (bx - pos.x) * t;
        const cy = pos.y + (by - pos.y) * t;
        const offsetX = 14;

        // Small tick line to cause text
        const textY = cy + (dir < 0 ? -8 : 14);
        g.append('line').attr('class', 'branch-line').attr('stroke-width', 1)
          .attr('x1', cx).attr('y1', cy).attr('x2', cx + offsetX + 6).attr('y2', textY);

        // Cause text
        const sevColor = cause.severity === 'danger' ? '#D72638' : cause.severity === 'warning' ? '#D98106' : '#64748B';
        const textG = g.append('g').attr('transform', `translate(${cx + offsetX + 8}, ${textY})`)
          .style('cursor', 'pointer')
          .on('click', () => { if (onCauseClick) onCauseClick(cause); });

        textG.append('circle').attr('r', 3).attr('cy', -4).attr('fill', sevColor);
        const truncated = cause.text.length > 22 ? cause.text.slice(0, 22) + '…' : cause.text;
        textG.append('text').attr('x', 8).attr('y', 0).attr('font-size', '10px').attr('fill', '#334155')
          .text(truncated);

        // Evidence count badge
        if (cause.evidence && cause.evidence.length) {
          textG.append('rect').attr('x', 8 + truncated.length * 5.5 + 4).attr('y', -10).attr('width', 16).attr('height', 14).attr('rx', 7).attr('fill', '#EEF4FF');
          textG.append('text').attr('x', 8 + truncated.length * 5.5 + 12).attr('y', 0).attr('font-size', '8px').attr('fill', '#0B5FFF').attr('text-anchor', 'middle').attr('font-weight', '600').text(cause.evidence.length);
        }
      });
    });

    // Title
    g.append('text').attr('x', spineStart).attr('y', -8).attr('font-size', '11px').attr('fill', '#0F172A').attr('font-weight', '700').text('鱼骨图 · 6M 分析');
  }

  // ============================================================
  //  D3: Evidence topology (force-directed graph)
  // ============================================================
  function renderTopology(containerId, topoData, onNodeClick) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = '';

    const width = container.clientWidth || 600;
    const height = 360;

    const svg = d3.select(container).append('svg')
      .attr('class', 'topo-svg')
      .attr('width', '100%').attr('height', height)
      .attr('viewBox', `0 0 ${width} ${height}`);

    // Gradient defs
    const defs = svg.append('defs');
    ['brand', 'success', 'danger', 'ems', 'lims', 'lms', 'bms', 'cmms', 'mes'].forEach(c => {
      const grad = defs.append('radialGradient').attr('id', `grad-${c}`);
      grad.append('stop').attr('offset', '0%').attr('stop-color', colorForType(c));
      grad.append('stop').attr('offset', '100%').attr('stop-color', colorForType(c)).attr('stop-opacity', 0.7);
    });

    const g = svg.append('g');

    // Zoom
    const zoom = d3.zoom().scaleExtent([0.5, 3]).on('zoom', (e) => g.attr('transform', e.transform));
    svg.call(zoom);

    const nodes = topoData.nodes.map(d => ({ ...d }));
    const links = topoData.links.map(d => ({ ...d }));

    const sim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id(d => d.id).distance(d => d.target.type === 'root' ? 80 : d.target.type === 'conclusion' ? 70 : 90).strength(0.6))
      .force('charge', d3.forceManyBody().strength(-280))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collide', d3.forceCollide().radius(d => nodeRadius(d) + 8));

    const link = g.append('g').selectAll('line').data(links).join('line')
      .attr('class', 'link').attr('marker-end', 'url(#arrow)');

    // Arrow marker
    defs.append('marker').attr('id', 'arrow').attr('viewBox', '0 -5 10 10').attr('refX', 18).attr('refY', 0)
      .attr('markerWidth', 6).attr('markerHeight', 6).attr('orient', 'auto')
      .append('path').attr('d', 'M0,-5L10,0L0,5').attr('fill', '#CBD5E1');

    const node = g.append('g').selectAll('g').data(nodes).join('g').attr('class', 'node')
      .call(d3.drag().on('start', dragStart).on('drag', dragging).on('end', dragEnd));

    node.append('circle')
      .attr('r', nodeRadius)
      .attr('fill', d => `url(#grad-${d.type === 'root' ? 'danger' : d.type === 'conclusion' ? 'brand' : (d.source || '').toLowerCase()})`)
      .attr('stroke', d => d.type === 'root' ? '#B81E2E' : d.type === 'conclusion' ? '#0B5FFF' : '#CBD5E1');

    node.append('text').attr('dy', 3).attr('fill', d => d.type === 'root' || d.type === 'conclusion' ? '#fff' : '#334155').attr('font-weight', d => d.type === 'root' || d.type === 'conclusion' ? '700' : '500');

    node.selectAll('text').text(d => d.label);

    node.on('click', (e, d) => { if (onNodeClick) onNodeClick(d); })
      .append('title').text(d => `${d.id}: ${d.label}`);

    sim.on('tick', () => {
      link.attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y);
      node.attr('transform', d => `translate(${d.x},${d.y})`);
    });

    function nodeRadius(d) { return d.type === 'root' ? 22 : d.type === 'conclusion' ? 18 : 14; }
    function colorForType(c) {
      const map = { brand: '#0B5FFF', success: '#0E7C66', danger: '#D72638', ems: '#F59E0B', lims: '#0B5FFF', lms: '#D72638', bms: '#0284C7', cmms: '#7C3AED', mes: '#0E7C66' };
      return map[c] || '#64748B';
    }
    function dragStart(e, d) { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }
    function dragging(e, d) { d.fx = e.x; d.fy = e.y; }
    function dragEnd(e, d) { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }

    // Legend
    const legend = svg.append('g').attr('transform', `translate(12, ${height - 80})`);
    const legendItems = [
      { label: '证据节点', color: '#94A3B8', r: 7 },
      { label: '中间结论', color: '#0B5FFF', r: 9 },
      { label: '根因', color: '#D72638', r: 11 },
    ];
    legendItems.forEach((item, i) => {
      const ly = i * 22;
      legend.append('circle').attr('cx', 8).attr('cy', ly).attr('r', item.r).attr('fill', item.color).attr('opacity', 0.85);
      legend.append('text').attr('x', 22).attr('y', ly + 3).attr('font-size', '10px').attr('fill', '#334155').text(item.label);
    });
  }

  // ============================================================
  //  D3: Heatmap (room x deviation type)
  // ============================================================
  function renderHeatmap(containerId, heatData, onCellClick) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = '';

    const margin = { top: 30, right: 20, bottom: 40, left: 140 };
    const width = (container.clientWidth || 700) - margin.left - margin.right;
    const cellW = width / heatData.types.length;
    const cellH = 28;
    const height = heatData.rooms.length * cellH + margin.top + margin.bottom;

    const svg = d3.select(container).append('svg')
      .attr('width', '100%').attr('height', height)
      .attr('viewBox', `0 0 ${width + margin.left + margin.right} ${height}`);

    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    const maxVal = d3.max(heatData.matrix.flat());
    const color = d3.scaleSequential().interpolator(t => {
      // white -> amber -> red
      if (t < 0.5) return d3.interpolateRgb('#FFFFFF', '#FEF3E2')(t * 2);
      return d3.interpolateRgb('#FEF3E2', '#D72638')((t - 0.5) * 2);
    }).domain([0, maxVal]);

    // X axis (types)
    g.append('g').selectAll('text').data(heatData.types).join('text')
      .attr('x', (d, i) => i * cellW + cellW / 2).attr('y', -8)
      .attr('text-anchor', 'middle').attr('font-size', '10px').attr('fill', '#64748B')
      .text(d => d.length > 6 ? d.slice(0, 6) + '…' : d);

    // Y axis (rooms)
    g.append('g').selectAll('text').data(heatData.rooms).join('text')
      .attr('x', -8).attr('y', (d, i) => i * cellH + cellH / 2 + 3)
      .attr('text-anchor', 'end').attr('font-size', '10px').attr('font-family', 'JetBrains Mono').attr('fill', '#334155')
      .text(d => d);

    // Cells
    heatData.rooms.forEach((room, i) => {
      heatData.types.forEach((type, j) => {
        const val = heatData.matrix[i][j];
        const cellG = g.append('g').attr('transform', `translate(${j * cellW}, ${i * cellH})`);
        cellG.append('rect').attr('class', 'heatmap-cell')
          .attr('width', cellW - 3).attr('height', cellH - 3).attr('rx', 4)
          .attr('fill', color(val)).attr('stroke', '#fff').attr('stroke-width', 1.5)
          .on('click', () => { if (onCellClick) onCellClick({ room, type, value: val }); });
        if (val > 0) {
          cellG.append('text').attr('x', (cellW - 3) / 2).attr('y', (cellH - 3) / 2 + 4)
            .attr('text-anchor', 'middle').attr('font-size', '10px').attr('font-weight', '600')
            .attr('fill', val > maxVal * 0.6 ? '#fff' : '#334155').text(val);
        }
      });
    });
  }

  // ============================================================
  //  D3: Knowledge clusters scatter (UMAP-like)
  // ============================================================
  function renderClusters(containerId, clusters) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = '';

    const width = container.clientWidth || 600;
    const height = 340;

    const svg = d3.select(container).append('svg').attr('width', '100%').attr('height', height).attr('viewBox', `0 0 ${width} ${height}`);

    clusters.forEach(cl => {
      const cx = cl.x * width;
      const cy = cl.y * height;
      const r = 18 + cl.count * 1.5;

      // Halo
      svg.append('circle').attr('cx', cx).attr('cy', cy).attr('r', r + 12)
        .attr('fill', cl.color).attr('opacity', 0.08);
      svg.append('circle').attr('cx', cx).attr('cy', cy).attr('r', r)
        .attr('fill', cl.color).attr('opacity', 0.18).attr('stroke', cl.color).attr('stroke-width', 1.5).attr('stroke-opacity', 0.4);

      // Label
      svg.append('text').attr('x', cx).attr('y', cy - 2).attr('text-anchor', 'middle')
        .attr('font-size', '10px').attr('font-weight', '600').attr('fill', '#0F172A').text(cl.name);
      svg.append('text').attr('x', cx).attr('y', cy + 12).attr('text-anchor', 'middle')
        .attr('font-size', '14px').attr('font-weight', '700').attr('fill', cl.color).text(cl.count);
    });
  }

  // ============================================================
  //  D3: Audit hash chain visualization
  // ============================================================
  function renderHashChain(containerId, logs) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = '';

    const width = container.clientWidth || 800;
    const nodeSpacing = 56;
    const height = 90;

    const svg = d3.select(container).append('svg').attr('width', '100%').attr('height', height);

    const visible = logs.slice(-8);
    const totalW = visible.length * nodeSpacing;
    const startX = (width - totalW) / 2;

    // Connecting line
    svg.append('line').attr('x1', startX).attr('y1', 45).attr('x2', startX + totalW - nodeSpacing).attr('y2', 45)
      .attr('stroke', '#0E7C66').attr('stroke-width', 2).attr('stroke-dasharray', '4 3');

    visible.forEach((log, i) => {
      const x = startX + i * nodeSpacing;
      const g = svg.append('g').attr('transform', `translate(${x}, 45)`);

      g.append('circle').attr('r', 10).attr('fill', '#fff').attr('stroke', '#0E7C66').attr('stroke-width', 2);
      g.append('text').attr('text-anchor', 'middle').attr('y', 3).attr('font-size', '9px').attr('font-weight', '700').attr('fill', '#0E7C66').attr('font-family', 'JetBrains Mono').text(log.seq);
      g.append('text').attr('text-anchor', 'middle').attr('y', 28).attr('font-size', '8px').attr('fill', '#64748B').attr('font-family', 'JetBrains Mono')
        .text(log.hash.slice(6, 14));

      if (i < visible.length - 1) {
        g.append('text').attr('x', nodeSpacing / 2).attr('y', -8).attr('text-anchor', 'middle').attr('font-size', '9px').attr('fill', '#94A3B8').attr('font-family', 'JetBrains Mono').text('→ hash');
      }
    });

    svg.append('text').attr('x', 12).attr('y', 14).attr('font-size', '10px').attr('fill', '#64748B').text('链式 hash 防篡改 · 最近 8 条');
  }

  return {
    setupChartDefaults,
    renderDeviationTrend,
    renderEMTrend,
    renderHVACTrend,
    renderCAPARadar,
    renderFishbone,
    renderTopology,
    renderHeatmap,
    renderClusters,
    renderHashChain,
  };
})();
