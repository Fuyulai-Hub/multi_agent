/* ============================================================
   星选商城 StarMart —— 演示用电商前端逻辑
   纯浏览器端：商品/购物车/订单/库存全部落在 localStorage
   ============================================================ */
(function () {
  'use strict';

  var STORAGE_KEY = 'starmart.v1';

  var CATEGORIES = ['数码影音', '家居生活', '美妆个护', '运动户外', '食品饮料'];

  var COUPONS = {
    SAVE10: { label: '满 100 减 20', type: 'amount', value: 20, min: 100 },
    NEW30: { label: '满 300 减 30', type: 'amount', value: 30, min: 300 },
    VIP9: { label: '满 500 享 9 折（最高减 100）', type: 'percent', value: 0.9, min: 500, cap: 100 }
  };

  var SHIPPING = {
    standard: { label: '标准配送', fee: 10, freeOver: 99 },
    express: { label: '次日达', fee: 15, freeOver: Infinity }
  };

  var PALETTE = [
    ['#eef2ff', '#dfe6ff'], ['#fff1e8', '#ffe0cc'], ['#e9f7f0', '#d3f0e2'],
    ['#fdeef7', '#fbd8ea'], ['#eef7fb', '#d6eef8'], ['#f5f0ff', '#e8ddff'],
    ['#fff8e1', '#ffeeb8'], ['#f0f4f8', '#dfe6ee']
  ];

  var SEED_PRODUCTS = [
    { id: 'p1', name: '星选 Air 主动降噪耳机', category: '数码影音', price: 899, origPrice: 1099, stock: 32, sales: 1268, rating: 4.9, emoji: '🎧', tags: ['爆款', '降噪'], desc: '38dB 主动降噪，单次续航 8 小时，通话降噪双麦克风。' },
    { id: 'p2', name: '轻羽无线蓝牙耳机', category: '数码影音', price: 329, origPrice: 399, stock: 58, sales: 942, rating: 4.7, emoji: '🎵', tags: ['半入耳'], desc: '单只 3.8g，久戴不痛，支持双设备无缝切换。' },
    { id: 'p3', name: '14 英寸轻薄笔记本 Pro', category: '数码影音', price: 5499, origPrice: 5999, stock: 12, sales: 213, rating: 4.8, emoji: '💻', tags: ['包邮', '办公'], desc: '2.8K 120Hz 屏，16G 内存 + 512G 固态，重量 1.32kg。' },
    { id: 'p4', name: '4K 运动相机套装', category: '数码影音', price: 1599, origPrice: 1899, stock: 20, sales: 356, rating: 4.6, emoji: '📷', tags: ['防抖'], desc: '超强增稳 3.0，裸机 10 米防水，含两块电池。' },
    { id: 'p5', name: '无线机械键盘 87 键', category: '数码影音', price: 459, origPrice: 529, stock: 41, sales: 688, rating: 4.7, emoji: '⌨️', tags: ['热插拔'], desc: 'Gasket 结构，三模连接，热插拔轴座。' },

    { id: 'p6', name: '云朵绒四件套（1.8m 床）', category: '家居生活', price: 399, origPrice: 499, stock: 26, sales: 517, rating: 4.8, emoji: '🛏️', tags: ['亲肤'], desc: '60 支长绒棉，活性印染，越洗越软。' },
    { id: 'p7', name: '香薰加湿器 500ml', category: '家居生活', price: 169, origPrice: 219, stock: 64, sales: 1302, rating: 4.5, emoji: '🕯️', tags: ['静音'], desc: '超声波雾化，28dB 静音，缺水自动断电。' },
    { id: 'p8', name: '全棉吸水浴巾两条装', category: '家居生活', price: 89, origPrice: 129, stock: 88, sales: 2140, rating: 4.6, emoji: '🧺', tags: ['A 类'], desc: 'A 类婴幼级面料，强吸水不掉毛。' },
    { id: 'p9', name: '北欧风桌面收纳架', category: '家居生活', price: 129, origPrice: 159, stock: 47, sales: 431, rating: 4.4, emoji: '🗄️', tags: ['免安装'], desc: '三层实木，承重 15kg，免工具安装。' },

    { id: 'p10', name: '烟酰胺精华原液 30ml', category: '美妆个护', price: 219, origPrice: 269, stock: 73, sales: 1896, rating: 4.7, emoji: '🧴', tags: ['提亮'], desc: '5% 烟酰胺 + 神经酰胺，稳定提亮不刺激。' },
    { id: 'p11', name: '氨基酸温和洁面乳', category: '美妆个护', price: 69, origPrice: 89, stock: 120, sales: 3320, rating: 4.6, emoji: '🧼', tags: ['敏感肌'], desc: '弱酸性配方，洗后不紧绷。' },
    { id: 'p12', name: '声波电动牙刷', category: '美妆个护', price: 249, origPrice: 299, stock: 35, sales: 764, rating: 4.5, emoji: '🦷', tags: ['长续航'], desc: '五档模式，一次充电用 60 天。' },

    { id: 'p13', name: '轻量缓震跑鞋 Cloud', category: '运动户外', price: 459, origPrice: 599, stock: 54, sales: 872, rating: 4.8, emoji: '👟', tags: ['回弹'], desc: '超临界发泡中底，单只 215g，适合日常 5-10km。' },
    { id: 'p14', name: '城市通勤双肩包 22L', category: '运动户外', price: 329, origPrice: 399, stock: 39, sales: 604, rating: 4.6, emoji: '🎒', tags: ['防泼水'], desc: '独立 16 寸电脑仓，防泼水面料，背板透气。' },
    { id: 'p15', name: '折叠露营椅（承重 150kg）', category: '运动户外', price: 189, origPrice: 239, stock: 0, sales: 288, rating: 4.4, emoji: '🪑', tags: ['便携'], desc: '一秒收纳，铝合金骨架，附收纳袋。' },
    { id: 'p16', name: '瑜伽垫 TPE 加厚 8mm', category: '运动户外', price: 139, origPrice: 179, stock: 62, sales: 941, rating: 4.5, emoji: '🧘', tags: ['防滑'], desc: '双面防滑纹理，无味环保 TPE。' },

    { id: 'p17', name: '云南手冲咖啡豆 500g', category: '食品饮料', price: 89, origPrice: 118, stock: 96, sales: 2765, rating: 4.9, emoji: '☕', tags: ['当季新豆'], desc: '中度烘焙，坚果与黑巧风味，接单后现磨发货。' },
    { id: 'p18', name: '每日坚果混合装 30 包', category: '食品饮料', price: 109, origPrice: 139, stock: 140, sales: 4120, rating: 4.7, emoji: '🥜', tags: ['独立装'], desc: '6 种坚果果干，独立小包，办公室囤货首选。' },
    { id: 'p19', name: '冷萃茶包组合 24 袋', category: '食品饮料', price: 59, origPrice: 79, stock: 0, sales: 1533, rating: 4.5, emoji: '🍵', tags: ['0 糖'], desc: '白桃乌龙 / 茉莉绿茶 / 蜜桃红茶，冷水 5 分钟出味。' }
  ];

  /* ------------------------------------------------------------ 工具 */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function round2(n) { return Math.round(n * 100) / 100; }
  function money(n) {
    return '¥' + Number(n).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }
  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function uid(prefix) { return prefix + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }
  function orderNo() {
    var d = new Date();
    var stamp = d.getFullYear() + String(d.getMonth() + 1).padStart(2, '0') + String(d.getDate()).padStart(2, '0');
    return 'SM' + stamp + '-' + String(Math.floor(1000 + Math.random() * 9000));
  }

  var toastTimer = null;
  function toast(message) {
    var node = $('#toast');
    node.textContent = message;
    node.hidden = false;
    requestAnimationFrame(function () { node.classList.add('show'); });
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      node.classList.remove('show');
      setTimeout(function () { node.hidden = true; }, 220);
    }, 1900);
  }

  /* ------------------------------------------------------------ 数据层 */
  function seedState() {
    var products = SEED_PRODUCTS.map(function (item, index) {
      var pair = PALETTE[index % PALETTE.length];
      return Object.assign({}, item, { active: true, g1: pair[0], g2: pair[1] });
    });
    return { products: products, cart: [], orders: [], seq: 1 };
  }

  function loadState() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (parsed && Array.isArray(parsed.products) && Array.isArray(parsed.cart) && Array.isArray(parsed.orders)) {
          return parsed;
        }
      }
    } catch (err) { /* 存储不可用/内容损坏时回落到种子数据 */ }
    return seedState();
  }

  function saveState() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
    catch (err) { toast('本地存储不可用，改动只在本次会话生效'); }
  }

  var state = loadState();

  var ui = {
    view: 'shop',
    category: '全部',
    query: '',
    sort: 'default',
    priceMin: null,
    priceMax: null,
    inStockOnly: false,
    shipping: 'standard',
    coupon: '',
    detailId: null,
    detailQty: 1
  };

  /* ------------------------------------------------------------ 查询 */
  function productById(id) {
    for (var i = 0; i < state.products.length; i++) {
      if (state.products[i].id === id) return state.products[i];
    }
    return null;
  }

  function cartLines() {
    return state.cart.map(function (line) {
      var product = productById(line.productId);
      return product ? { product: product, qty: line.qty } : null;
    }).filter(Boolean);
  }

  function cartCount() {
    return state.cart.reduce(function (sum, line) { return sum + line.qty; }, 0);
  }

  function visibleProducts() {
    var query = ui.query.trim().toLowerCase();
    var list = state.products.filter(function (product) {
      if (!product.active) return false;
      if (ui.category !== '全部' && product.category !== ui.category) return false;
      if (ui.inStockOnly && product.stock <= 0) return false;
      if (ui.priceMin != null && product.price < ui.priceMin) return false;
      if (ui.priceMax != null && product.price > ui.priceMax) return false;
      if (query) {
        var hay = (product.name + ' ' + product.category + ' ' + product.desc + ' ' + (product.tags || []).join(' ')).toLowerCase();
        if (hay.indexOf(query) < 0) return false;
      }
      return true;
    });
    var sorters = {
      sales: function (a, b) { return b.sales - a.sales; },
      'price-asc': function (a, b) { return a.price - b.price; },
      'price-desc': function (a, b) { return b.price - a.price; },
      rating: function (a, b) { return b.rating - a.rating; },
      default: function (a, b) { return b.sales * b.rating - a.sales * a.rating; }
    };
    return list.sort(sorters[ui.sort] || sorters.default);
  }

  function totals() {
    var subtotal = cartLines().reduce(function (sum, line) {
      return sum + line.product.price * line.qty;
    }, 0);
    var coupon = COUPONS[ui.coupon];
    var discount = 0;
    if (coupon && subtotal >= coupon.min) {
      discount = coupon.type === 'amount'
        ? coupon.value
        : Math.min(round2(subtotal * (1 - coupon.value)), coupon.cap || Infinity);
    }
    var base = Math.max(0, subtotal - discount);
    var rule = SHIPPING[ui.shipping];
    var shipping = base >= rule.freeOver ? 0 : rule.fee;
    return { subtotal: subtotal, discount: round2(discount), shipping: shipping, total: round2(base + shipping) };
  }

  function pendingOrderCount() {
    return state.orders.filter(function (order) { return order.status === 'pending'; }).length;
  }

  /* ------------------------------------------------------------ 渲染：商品列表 */
  function renderCategories() {
    var host = $('#category-list');
    var counts = {};
    state.products.forEach(function (p) {
      if (p.active) counts[p.category] = (counts[p.category] || 0) + 1;
    });
    var all = ['全部'].concat(CATEGORIES);
    host.innerHTML = all.map(function (name) {
      var count = name === '全部'
        ? state.products.filter(function (p) { return p.active; }).length
        : (counts[name] || 0);
      return '<button class="cat-item' + (ui.category === name ? ' active' : '') + '" data-action="pick-category" data-value="' + esc(name) + '">'
        + esc(name) + '<em>' + count + '</em></button>';
    }).join('');
  }

  function cardHtml(product) {
    var out = product.stock <= 0;
    var badge = out ? '<span class="badge badge-soldout">已售罄</span>'
      : (product.tags && product.tags[0] ? '<span class="badge">' + esc(product.tags[0]) + '</span>' : '');
    var stockClass = product.stock > 0 && product.stock <= 5 ? ' low' : '';
    var button = out
      ? '<button class="btn btn-ghost btn-sm" disabled>暂时缺货</button>'
      : '<button class="btn btn-primary btn-sm" data-action="add-cart" data-id="' + product.id + '">加入购物车</button>';
    return '<article class="card' + (out ? ' is-out' : '') + '">'
      + '<div class="card-cover" style="--g1:' + product.g1 + ';--g2:' + product.g2 + '" data-action="open-product" data-id="' + product.id + '">'
      + badge + '<span class="emoji">' + esc(product.emoji) + '</span></div>'
      + '<div class="card-body">'
      + '<h3 class="card-name" data-action="open-product" data-id="' + product.id + '">' + esc(product.name) + '</h3>'
      + '<p class="card-desc">' + esc(product.desc) + '</p>'
      + '<div class="card-meta"><span class="rating">★ ' + product.rating.toFixed(1) + '</span><span>月销 ' + product.sales + '</span></div>'
      + '<div class="card-foot"><div class="price"><em>¥</em>' + product.price
      + (product.origPrice ? '<span class="price-original">¥' + product.origPrice + '</span>' : '') + '</div>'
      + button + '</div>'
      + '<div class="card-stock' + stockClass + '">' + (out ? '补货中' : '库存 ' + product.stock + ' 件') + '</div>'
      + '</div></article>';
  }

  function renderProducts() {
    var list = visibleProducts();
    $('#product-grid').innerHTML = list.map(cardHtml).join('');
    $('#empty-state').hidden = list.length > 0;
    $('#result-count').textContent = list.length;
    $('#result-query').textContent = ui.query ? '匹配「' + ui.query + '」' : '';
  }

  /* ------------------------------------------------------------ 渲染：购物车 */
  function renderCart() {
    var lines = cartLines();
    var count = cartCount();
    $('#cart-count').textContent = count;
    $('#cart-summary-count').textContent = count ? '（' + count + ' 件）' : '';
    $('#cart-total').textContent = money(totals().subtotal);
    $('#cart-empty').hidden = lines.length > 0;

    $('#cart-items').innerHTML = lines.map(function (line) {
      var p = line.product;
      return '<div class="cart-item">'
        + '<div class="cart-thumb" style="--g1:' + p.g1 + ';--g2:' + p.g2 + '">' + esc(p.emoji) + '</div>'
        + '<div class="cart-info">'
        + '<div class="name">' + esc(p.name) + '</div>'
        + '<div class="meta"><div class="price"><em>¥</em>' + p.price + '</div>'
        + '<div class="qty">'
        + '<button class="qty-btn" data-action="qty-dec" data-id="' + p.id + '" aria-label="减少">−</button>'
        + '<span class="qty-num">' + line.qty + '</span>'
        + '<button class="qty-btn" data-action="qty-inc" data-id="' + p.id + '" aria-label="增加"'
        + (line.qty >= p.stock ? ' disabled' : '') + '>+</button>'
        + '</div></div></div>'
        + '<div style="display:flex;flex-direction:column;align-items:flex-end;justify-content:space-between">'
        + '<button class="icon-btn" data-action="remove-line" data-id="' + p.id + '" aria-label="删除">✕</button>'
        + '<span class="cart-line-total">' + money(p.price * line.qty) + '</span>'
        + '</div></div>';
    }).join('');
  }

  /* ------------------------------------------------------------ 渲染：结算 */
  function renderCheckout() {
    var lines = cartLines();
    var t = totals();
    $('#checkout-items').innerHTML = lines.map(function (line) {
      var p = line.product;
      return '<div class="checkout-item">'
        + '<div class="thumb" style="--g1:' + p.g1 + ';--g2:' + p.g2 + '">' + esc(p.emoji) + '</div>'
        + '<span class="name">' + esc(p.name) + '</span>'
        + '<span class="qty">×' + line.qty + '</span>'
        + '<span class="amt">' + money(p.price * line.qty) + '</span></div>';
    }).join('');
    $('#sum-subtotal').textContent = money(t.subtotal);
    $('#sum-discount').textContent = '-' + money(t.discount);
    $('#sum-shipping').textContent = t.shipping === 0 ? '免运费' : money(t.shipping);
    $('#sum-total').textContent = money(t.total);

    var hint = $('#coupon-hint');
    var coupon = COUPONS[ui.coupon];
    if (!ui.coupon) {
      hint.textContent = '可用优惠码：SAVE10 / NEW30 / VIP9';
      hint.className = 'coupon-hint';
    } else if (coupon && t.subtotal >= coupon.min) {
      hint.textContent = '已使用「' + coupon.label + '」，优惠 ' + money(t.discount);
      hint.className = 'coupon-hint ok';
    } else {
      hint.textContent = '优惠码「' + ui.coupon + '」需满 ' + (coupon ? coupon.min : 0) + ' 元可用';
      hint.className = 'coupon-hint bad';
    }
  }

  /* ------------------------------------------------------------ 渲染：订单 */
  var STATUS_TEXT = { pending: '待发货', shipped: '已发货', done: '已完成', canceled: '已取消' };

  function statusClass(status) {
    return { pending: 'status-pending', shipped: 'status-shipped', done: 'status-done', canceled: 'status-canceled' }[status];
  }

  function renderOrders() {
    var list = state.orders.slice().sort(function (a, b) { return b.createdAt - a.createdAt; });
    $('#orders-empty').hidden = list.length > 0;
    $('#order-list').innerHTML = list.map(function (order) {
      var items = order.items.map(function (item) {
        var p = productById(item.productId);
        var g1 = p ? p.g1 : '#eef1f5';
        var g2 = p ? p.g2 : '#dfe4ea';
        var emoji = p ? p.emoji : '📦';
        return '<div class="order-item"><div class="thumb" style="--g1:' + g1 + ';--g2:' + g2 + '">' + esc(emoji) + '</div>'
          + '<span class="name">' + esc(item.name) + '</span><span class="qty">×' + item.qty + '</span>'
          + '<span>' + money(item.price * item.qty) + '</span></div>';
      }).join('');
      var actions = '';
      if (order.status === 'pending') {
        actions += '<button class="btn btn-ghost btn-sm" data-action="cancel-order" data-id="' + order.id + '">取消订单</button>';
      }
      actions += '<button class="btn btn-ghost btn-sm" data-action="rebuy" data-id="' + order.id + '">再次购买</button>';
      return '<article class="order-card">'
        + '<div class="order-head"><b>' + esc(order.id) + '</b>'
        + '<span>' + new Date(order.createdAt).toLocaleString('zh-CN', { hour12: false }) + '</span>'
        + '<span class="order-status ' + statusClass(order.status) + '">' + STATUS_TEXT[order.status] + '</span></div>'
        + '<div class="order-body">' + items + '</div>'
        + '<div class="order-foot"><span class="addr">' + esc(order.customer.name) + ' · ' + esc(order.customer.phone) + ' · ' + esc(order.customer.address) + '</span>'
        + '<span class="amount">合计 <b>' + money(order.total) + '</b></span>' + actions + '</div>'
        + '</article>';
    }).join('');

    var badge = $('#order-badge');
    var pending = pendingOrderCount();
    badge.hidden = pending === 0;
  }

  /* ------------------------------------------------------------ 渲染：管理后台 */
  function renderAdmin() {
    var active = state.products.filter(function (p) { return p.active; });
    var stock = state.products.reduce(function (sum, p) { return sum + p.stock; }, 0);
    var valid = state.orders.filter(function (o) { return o.status !== 'canceled'; });
    var revenue = valid.reduce(function (sum, o) { return sum + o.total; }, 0);
    $('#stat-row').innerHTML = [
      ['在售商品', active.length, ''],
      ['库存总量', stock, ''],
      ['订单数', state.orders.length, ''],
      ['成交金额', money(revenue), 'brand']
    ].map(function (row) {
      return '<div class="stat-card ' + row[2] + '"><span>' + row[0] + '</span><b>' + row[1] + '</b></div>';
    }).join('');

    $('#admin-product-count').textContent = '（' + state.products.length + ' 个 SKU）';
    $('#admin-product-rows').innerHTML = state.products.map(function (p) {
      return '<tr>'
        + '<td class="cell-name">' + esc(p.emoji) + ' ' + esc(p.name) + '</td>'
        + '<td>' + esc(p.category) + '</td>'
        + '<td><input class="mini-input" type="number" min="0" step="0.01" value="' + p.price + '" data-field="price" data-id="' + p.id + '" aria-label="价格"></td>'
        + '<td><input class="mini-input" type="number" min="0" step="1" value="' + p.stock + '" data-field="stock" data-id="' + p.id + '" aria-label="库存"></td>'
        + '<td>' + p.sales + '</td>'
        + '<td><span class="tag ' + (p.active ? 'tag-on' : 'tag-off') + '">' + (p.active ? '在售' : '已下架') + '</span></td>'
        + '<td><div class="row-actions">'
        + '<button class="btn btn-ghost btn-sm" data-action="toggle-active" data-id="' + p.id + '">' + (p.active ? '下架' : '上架') + '</button>'
        + '<button class="btn btn-ghost btn-sm" data-action="delete-product" data-id="' + p.id + '">删除</button>'
        + '</div></td></tr>';
    }).join('');

    var orders = state.orders.slice().sort(function (a, b) { return b.createdAt - a.createdAt; });
    $('#admin-order-count').textContent = '（' + orders.length + ' 笔）';
    $('#admin-order-rows').innerHTML = orders.length ? orders.map(function (order) {
      var count = order.items.reduce(function (sum, item) { return sum + item.qty; }, 0);
      var actions = '';
      if (order.status === 'pending') {
        actions += '<button class="btn btn-ghost btn-sm" data-action="ship-order" data-id="' + order.id + '">发货</button>';
      }
      if (order.status === 'shipped') {
        actions += '<button class="btn btn-ghost btn-sm" data-action="finish-order" data-id="' + order.id + '">完成</button>';
      }
      actions += '<button class="btn btn-ghost btn-sm" data-action="cancel-order" data-id="' + order.id + '"'
        + (order.status === 'pending' ? '' : ' disabled') + '>取消</button>';
      return '<tr><td>' + esc(order.id) + '</td>'
        + '<td>' + new Date(order.createdAt).toLocaleString('zh-CN', { hour12: false }) + '</td>'
        + '<td>' + esc(order.customer.name) + '</td>'
        + '<td>' + count + '</td>'
        + '<td>' + money(order.total) + '</td>'
        + '<td><span class="order-status ' + statusClass(order.status) + '">' + STATUS_TEXT[order.status] + '</span></td>'
        + '<td><div class="row-actions">' + actions + '</div></td></tr>';
    }).join('') : '<tr><td colspan="7" class="empty-row">暂无订单</td></tr>';
  }

  function renderAdminCategories() {
    $('#np-category').innerHTML = CATEGORIES.map(function (name) {
      return '<option value="' + esc(name) + '">' + esc(name) + '</option>';
    }).join('');
  }

  function renderAll() {
    renderCategories();
    renderProducts();
    renderCart();
    renderCheckout();
    renderOrders();
    renderAdmin();
  }

  /* ------------------------------------------------------------ 视图切换 */
  function setView(view) {
    ui.view = view;
    ['shop', 'checkout', 'orders', 'admin'].forEach(function (name) {
      $('#view-' + name).hidden = name !== view;
    });
    window.scrollTo({ top: 0, behavior: 'smooth' });
    if (view === 'checkout') renderCheckout();
    if (view === 'orders') renderOrders();
    if (view === 'admin') renderAdmin();
  }

  /* ------------------------------------------------------------ 购物车操作 */
  function addToCart(id, qty) {
    var product = productById(id);
    if (!product) return;
    if (product.stock <= 0) { toast('「' + product.name + '」暂时缺货'); return; }
    var line = state.cart.filter(function (item) { return item.productId === id; })[0];
    var want = (line ? line.qty : 0) + (qty || 1);
    if (want > product.stock) {
      toast('库存只剩 ' + product.stock + ' 件');
      want = product.stock;
    }
    if (line) line.qty = want; else state.cart.push({ productId: id, qty: want });
    saveState();
    renderCart();
    renderProducts();
    toast('已加入购物车：' + product.name);
  }

  function changeQty(id, delta) {
    var line = state.cart.filter(function (item) { return item.productId === id; })[0];
    var product = productById(id);
    if (!line || !product) return;
    var next = line.qty + delta;
    if (next <= 0) { removeLine(id); return; }
    if (next > product.stock) { toast('库存只剩 ' + product.stock + ' 件'); return; }
    line.qty = next;
    saveState();
    renderCart();
    renderCheckout();
  }

  function removeLine(id) {
    state.cart = state.cart.filter(function (item) { return item.productId !== id; });
    saveState();
    renderCart();
    renderCheckout();
  }

  /* ------------------------------------------------------------ 购物车抽屉 */
  function openCart() {
    $('#cart-mask').hidden = false;
    $('#cart-drawer').classList.add('open');
    $('#cart-drawer').setAttribute('aria-hidden', 'false');
    requestAnimationFrame(function () { $('#cart-mask').classList.add('show'); });
    renderCart();
  }

  function closeCart() {
    $('#cart-drawer').classList.remove('open');
    $('#cart-drawer').setAttribute('aria-hidden', 'true');
    $('#cart-mask').classList.remove('show');
    setTimeout(function () { $('#cart-mask').hidden = true; }, 200);
  }

  /* ------------------------------------------------------------ 商品详情 */
  function openProduct(id) {
    var product = productById(id);
    if (!product) return;
    ui.detailId = id;
    ui.detailQty = product.stock > 0 ? 1 : 0;
    var out = product.stock <= 0;
    $('#pm-body').innerHTML = ''
      + '<div class="pm-cover" style="--g1:' + product.g1 + ';--g2:' + product.g2 + '"><span>' + esc(product.emoji) + '</span></div>'
      + '<div class="pm-info">'
      + '<div class="pm-cat">' + esc(product.category) + ' · 月销 ' + product.sales + ' · ★ ' + product.rating.toFixed(1) + '</div>'
      + '<h2 id="pm-name">' + esc(product.name) + '</h2>'
      + '<div class="pm-price"><span class="price"><em>¥</em>' + product.price + '</span>'
      + (product.origPrice ? '<span class="price-original">¥' + product.origPrice + '</span>' : '') + '</div>'
      + '<div class="pm-tags">' + (product.tags || []).map(function (tag) { return '<span class="pm-tag">' + esc(tag) + '</span>'; }).join('') + '</div>'
      + '<p class="pm-desc">' + esc(product.desc) + '</p>'
      + '<div class="pm-specs">'
      + '<div><span>分类</span><b>' + esc(product.category) + '</b></div>'
      + '<div><span>库存</span><b>' + (out ? '补货中' : product.stock + ' 件') + '</b></div>'
      + '<div><span>配送</span><b>满 99 包邮</b></div>'
      + '<div><span>售后</span><b>7 天无理由</b></div>'
      + '</div>'
      + '<div class="pm-actions">'
      + '<div class="qty"><button class="qty-btn" data-action="detail-dec" aria-label="减少">−</button>'
      + '<span class="qty-num">' + ui.detailQty + '</span>'
      + '<button class="qty-btn" data-action="detail-inc" aria-label="增加">+</button></div>'
      + '<button class="btn btn-primary" data-action="detail-add"' + (out ? ' disabled' : '') + '>加入购物车</button>'
      + '<button class="btn btn-ghost" data-action="detail-buy"' + (out ? ' disabled' : '') + '>立即购买</button>'
      + '</div></div>';

    $('#product-modal').hidden = false;
    $('#modal-mask').hidden = false;
    requestAnimationFrame(function () {
      $('#product-modal').classList.add('show');
      $('#modal-mask').classList.add('show');
    });
  }

  function closeModal() {
    $('#product-modal').classList.remove('show');
    $('#modal-mask').classList.remove('show');
    setTimeout(function () {
      $('#product-modal').hidden = true;
      $('#modal-mask').hidden = true;
    }, 180);
  }

  function openSuccess(order) {
    $('#success-text').innerHTML = '订单号 <b>' + esc(order.id) + '</b><br>应付金额 ' + money(order.total)
      + '，我们会尽快为你发货。';
    $('#success-modal').hidden = false;
    $('#success-mask').hidden = false;
    requestAnimationFrame(function () {
      $('#success-modal').classList.add('show');
      $('#success-mask').classList.add('show');
    });
  }

  function closeSuccess() {
    $('#success-modal').classList.remove('show');
    $('#success-mask').classList.remove('show');
    setTimeout(function () {
      $('#success-modal').hidden = true;
      $('#success-mask').hidden = true;
    }, 180);
  }

  /* ------------------------------------------------------------ 下单 */
  function fieldError(input, message) {
    var field = input.closest('.field');
    field.classList.toggle('invalid', Boolean(message));
    var tip = field.querySelector('.field-error');
    if (!message) { if (tip) tip.remove(); return; }
    if (!tip) {
      tip = document.createElement('span');
      tip.className = 'field-error';
      field.appendChild(tip);
    }
    tip.textContent = message;
  }

  function validateShipping() {
    var name = $('#ship-name');
    var phone = $('#ship-phone');
    var addr = $('#ship-addr');
    var ok = true;

    if (!name.value.trim()) { fieldError(name, '请填写收货人姓名'); ok = false; } else fieldError(name, '');
    if (!/^1[3-9]\d{9}$/.test(phone.value.trim())) { fieldError(phone, '请填写 11 位有效手机号'); ok = false; } else fieldError(phone, '');
    if (addr.value.trim().length < 5) { fieldError(addr, '地址至少 5 个字，请写清省市区与门牌'); ok = false; } else fieldError(addr, '');
    return ok;
  }

  function placeOrder() {
    var lines = cartLines();
    if (!lines.length) { toast('购物车是空的'); return; }
    if (!validateShipping()) { toast('请先补全收货信息'); return; }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.qty > line.product.stock) {
        toast('「' + line.product.name + '」库存不足（剩 ' + line.product.stock + ' 件），请调整数量');
        renderCart();
        return;
      }
    }

    var t = totals();
    var order = {
      id: orderNo(),
      createdAt: Date.now(),
      items: lines.map(function (line) {
        return { productId: line.product.id, name: line.product.name, price: line.product.price, qty: line.qty };
      }),
      subtotal: t.subtotal,
      discount: t.discount,
      shipping: t.shipping,
      total: t.total,
      coupon: COUPONS[ui.coupon] && t.discount > 0 ? ui.coupon : '',
      shippingMethod: SHIPPING[ui.shipping].label,
      customer: {
        name: $('#ship-name').value.trim(),
        phone: $('#ship-phone').value.trim(),
        address: $('#ship-addr').value.trim(),
        note: $('#ship-note').value.trim()
      },
      status: 'pending'
    };

    lines.forEach(function (line) {
      line.product.stock -= line.qty;
      line.product.sales += line.qty;
    });

    state.orders.push(order);
    state.cart = [];
    ui.coupon = '';
    $('#coupon-input').value = '';
    saveState();
    renderAll();
    openSuccess(order);
  }

  function cancelOrder(id) {
    var order = state.orders.filter(function (item) { return item.id === id; })[0];
    if (!order) return;
    if (order.status !== 'pending') { toast('该订单当前状态不可取消'); return; }
    order.items.forEach(function (item) {
      var product = productById(item.productId);
      if (product) {
        product.stock += item.qty;
        product.sales = Math.max(0, product.sales - item.qty);
      }
    });
    order.status = 'canceled';
    saveState();
    renderAll();
    toast('订单已取消，库存已返还');
  }

  function shipOrder(id, status) {
    var order = state.orders.filter(function (item) { return item.id === id; })[0];
    if (!order) return;
    order.status = status;
    saveState();
    renderAll();
    toast(status === 'shipped' ? '已标记发货' : '订单已完成');
  }

  function rebuy(id) {
    var order = state.orders.filter(function (item) { return item.id === id; })[0];
    if (!order) return;
    var added = 0;
    order.items.forEach(function (item) {
      var product = productById(item.productId);
      if (!product || !product.active || product.stock <= 0) return;
      var line = state.cart.filter(function (row) { return row.productId === product.id; })[0];
      var qty = Math.min(item.qty, product.stock);
      if (line) line.qty = Math.min(product.stock, line.qty + qty); else state.cart.push({ productId: product.id, qty: qty });
      added += qty;
    });
    saveState();
    renderCart();
    toast(added ? '已把 ' + added + ' 件商品加入购物车' : '这些商品暂时无法购买');
    if (added) openCart();
  }

  /* ------------------------------------------------------------ 管理后台操作 */
  function addProduct() {
    var name = $('#np-name').value.trim();
    var price = Number($('#np-price').value);
    var stock = Number($('#np-stock').value);
    var category = $('#np-category').value;
    var emoji = $('#np-emoji').value.trim() || '📦';
    var tags = $('#np-tags').value.split(/[,，]/).map(function (tag) { return tag.trim(); }).filter(Boolean);
    var desc = $('#np-desc').value.trim() || '新上架商品';

    if (!name) { toast('请填写商品名称'); return; }
    if (!(price > 0)) { toast('价格必须大于 0'); return; }
    if (!(stock >= 0) || !Number.isInteger(stock)) { toast('库存必须是大于等于 0 的整数'); return; }

    var pair = PALETTE[state.products.length % PALETTE.length];
    state.products.unshift({
      id: uid('p'), name: name, category: category, price: round2(price), origPrice: 0,
      stock: stock, sales: 0, rating: 5.0, emoji: emoji, tags: tags, desc: desc,
      active: true, g1: pair[0], g2: pair[1]
    });
    saveState();
    ['np-name', 'np-price', 'np-stock', 'np-emoji', 'np-tags', 'np-desc'].forEach(function (id) { $('#' + id).value = ''; });
    renderAll();
    toast('商品已上架');
  }

  function updateProductField(id, field, raw) {
    var product = productById(id);
    if (!product) return;
    var value = Number(raw);
    if (!isFinite(value) || value < 0) { toast('请输入合法的数值'); renderAdmin(); return; }
    product[field] = field === 'price' ? round2(value) : Math.floor(value);
    saveState();
    renderProducts();
    renderAdmin();
  }

  function toggleActive(id) {
    var product = productById(id);
    if (!product) return;
    product.active = !product.active;
    if (!product.active) state.cart = state.cart.filter(function (line) { return line.productId !== id; });
    saveState();
    renderAll();
    toast(product.active ? '已上架：' + product.name : '已下架：' + product.name);
  }

  function deleteProduct(id) {
    var product = productById(id);
    if (!product) return;
    if (!window.confirm('确定删除「' + product.name + '」吗？该操作不可撤销。')) return;
    state.products = state.products.filter(function (item) { return item.id !== id; });
    state.cart = state.cart.filter(function (line) { return line.productId !== id; });
    saveState();
    renderAll();
    toast('商品已删除');
  }

  function resetDemo() {
    if (!window.confirm('将清空所有商品改动、购物车与订单，恢复演示数据？')) return;
    state = seedState();
    ui.coupon = '';
    saveState();
    renderAll();
    toast('已重置演示数据');
  }

  /* ------------------------------------------------------------ 事件绑定 */
  var ACTIONS = {
    'go-shop': function () { closeCart(); closeModal(); setView('shop'); },
    'go-orders': function () { closeSuccess(); setView('orders'); },
    'go-admin': function () { setView('admin'); },
    'scroll-products': function () { $('#products').scrollIntoView({ behavior: 'smooth', block: 'start' }); },
    'open-cart': openCart,
    'close-cart': closeCart,
    'pick-category': function (el) {
      ui.category = el.dataset.value;
      renderCategories();
      renderProducts();
    },
    'clear-filters': function () {
      ui.category = '全部';
      ui.query = '';
      ui.priceMin = null;
      ui.priceMax = null;
      ui.inStockOnly = false;
      $('#search-input').value = '';
      $('#price-min').value = '';
      $('#price-max').value = '';
      $('#in-stock-only').checked = false;
      renderCategories();
      renderProducts();
    },
    'add-cart': function (el) { addToCart(el.dataset.id, 1); },
    'qty-inc': function (el) { changeQty(el.dataset.id, 1); },
    'qty-dec': function (el) { changeQty(el.dataset.id, -1); },
    'remove-line': function (el) { removeLine(el.dataset.id); },
    'clear-cart': function () {
      if (!state.cart.length) return;
      if (!window.confirm('清空购物车？')) return;
      state.cart = [];
      saveState();
      renderCart();
      renderCheckout();
      toast('购物车已清空');
    },
    'go-checkout': function () {
      if (!state.cart.length) { toast('购物车是空的'); return; }
      closeCart();
      setView('checkout');
    },
    'apply-coupon': function () {
      var code = $('#coupon-input').value.trim().toUpperCase();
      if (!code) { ui.coupon = ''; renderCheckout(); return; }
      if (!COUPONS[code]) {
        ui.coupon = '';
        toast('优惠码不存在');
        renderCheckout();
        $('#coupon-hint').textContent = '优惠码不存在，请核对后重试';
        $('#coupon-hint').className = 'coupon-hint bad';
        return;
      }
      ui.coupon = code;
      renderCheckout();
      var t = totals();
      if (t.discount > 0) toast('优惠码已生效，省 ' + money(t.discount));
      else toast('未达使用门槛，优惠暂未生效');
    },
    'place-order': placeOrder,
    'open-product': function (el) { openProduct(el.dataset.id); },
    'close-modal': closeModal,
    'detail-inc': function () {
      var product = productById(ui.detailId);
      if (!product) return;
      if (ui.detailQty >= product.stock) { toast('库存只剩 ' + product.stock + ' 件'); return; }
      ui.detailQty += 1;
      openProduct(ui.detailId);
    },
    'detail-dec': function () {
      if (ui.detailQty <= 1) return;
      ui.detailQty -= 1;
      openProduct(ui.detailId);
    },
    'detail-add': function () {
      addToCart(ui.detailId, ui.detailQty);
      closeModal();
    },
    'detail-buy': function () {
      addToCart(ui.detailId, ui.detailQty);
      closeModal();
      closeCart();
      setView('checkout');
    },
    'cancel-order': function (el) { cancelOrder(el.dataset.id); },
    'rebuy': function (el) { rebuy(el.dataset.id); },
    'ship-order': function (el) { shipOrder(el.dataset.id, 'shipped'); },
    'finish-order': function (el) { shipOrder(el.dataset.id, 'done'); },
    'add-product': addProduct,
    'toggle-active': function (el) { toggleActive(el.dataset.id); },
    'delete-product': function (el) { deleteProduct(el.dataset.id); },
    'reset-demo': resetDemo,
    'close-success': function () { closeSuccess(); setView('shop'); }
  };

  document.addEventListener('click', function (event) {
    var target = event.target.closest('[data-action]');
    if (!target) return;
    var handler = ACTIONS[target.dataset.action];
    if (!handler) return;
    event.preventDefault();
    handler(target, event);
  });

  document.addEventListener('input', function (event) {
    var el = event.target;
    if (el.id === 'search-input') {
      ui.query = el.value;
      renderProducts();
    }
  });

  document.addEventListener('change', function (event) {
    var el = event.target;
    if (el.id === 'sort-select') { ui.sort = el.value; renderProducts(); return; }
    if (el.id === 'in-stock-only') { ui.inStockOnly = el.checked; renderProducts(); return; }
    if (el.id === 'price-min' || el.id === 'price-max') {
      var value = el.value === '' ? null : Number(el.value);
      if (el.id === 'price-min') ui.priceMin = value; else ui.priceMax = value;
      renderProducts();
      return;
    }
    if (el.name === 'shipping') { ui.shipping = el.value; renderCheckout(); return; }
    if (el.dataset.field) { updateProductField(el.dataset.id, el.dataset.field, el.value); }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (!$('#product-modal').hidden) { closeModal(); return; }
    if (!$('#success-modal').hidden) { closeSuccess(); return; }
    if ($('#cart-drawer').classList.contains('open')) closeCart();
  });

  ['cart-mask', 'modal-mask', 'success-mask'].forEach(function (id) {
    $('#' + id).addEventListener('click', function () {
      if (id === 'cart-mask') closeCart();
      else if (id === 'modal-mask') closeModal();
      else closeSuccess();
    });
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && (event.target.id === 'search-input' || event.target.id === 'coupon-input')) {
      event.preventDefault();
      if (event.target.id === 'coupon-input') ACTIONS['apply-coupon']();
    }
  });

  /* ------------------------------------------------------------ 启动 */
  renderAdminCategories();
  saveState();          // 首次访问即把种子数据落盘，之后所有改动都以本地存储为准
  renderAll();
  setView('shop');
})();
