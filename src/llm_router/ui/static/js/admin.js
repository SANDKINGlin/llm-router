/**
 * Admin WebUI JavaScript 功能模块
 * 提供：HTMX增强、Token管理、错误处理
 */

(function() {
    'use strict';

    // ==================== Token 管理 ====================

    const TOKEN_KEY = 'admin_token';
    const TOKEN_EXPIRY_KEY = 'admin_token_expiry';

    /**
     * 保存 token 到 localStorage
     */
    function saveToken(token, expiresIn = 86400) {
        const expiry = Date.now() + (expiresIn * 1000);
        localStorage.setItem(TOKEN_KEY, token);
        localStorage.setItem(TOKEN_EXPIRY_KEY, expiry.toString());
    }

    /**
     * 获取存储的 token（如果未过期）
     */
    function getToken() {
        const token = localStorage.getItem(TOKEN_KEY);
        const expiry = localStorage.getItem(TOKEN_EXPIRY_KEY);

        if (!token || !expiry) {
            return null;
        }

        if (Date.now() > parseInt(expiry)) {
            clearToken();
            return null;
        }

        return token;
    }

    /**
     * 清除 token
     */
    function clearToken() {
        localStorage.removeItem(TOKEN_KEY);
        localStorage.removeItem(TOKEN_EXPIRY_KEY);
    }

    /**
     * 为所有请求添加 Authorization 头
     */
    function setupAuthHeaders() {
        document.body.addEventListener('htmx:beforeRequest', function(evt) {
            const token = getToken();
            if (token) {
                evt.detail.xhr.setRequestHeader('Authorization', `Bearer ${token}`);
            }
        });
    }

    // ==================== 错误处理 ====================

    /**
     * 显示错误提示
     */
    function showError(message) {
        removeAlerts();

        const alertDiv = document.createElement('div');
        alertDiv.className = 'alert alert-error';
        alertDiv.textContent = message;

        const container = document.querySelector('main') || document.body;
        container.insertBefore(alertDiv, container.firstChild);

        // 5秒后自动消失
        setTimeout(() => {
            if (alertDiv.parentNode) {
                alertDiv.remove();
            }
        }, 5000);
    }

    /**
     * 显示成功提示
     */
    function showSuccess(message) {
        removeAlerts();

        const alertDiv = document.createElement('div');
        alertDiv.className = 'alert alert-success';
        alertDiv.textContent = message;

        const container = document.querySelector('main') || document.body;
        container.insertBefore(alertDiv, container.firstChild);

        // 3秒后自动消失
        setTimeout(() => {
            if (alertDiv.parentNode) {
                alertDiv.remove();
            }
        }, 3000);
    }

    /**
     * 移除所有提示
     */
    function removeAlerts() {
        const alerts = document.querySelectorAll('.alert');
        alerts.forEach(alert => alert.remove());
    }

    /**
     * 处理 HTMX 错误响应
     */
    function setupErrorHandler() {
        document.body.addEventListener('htmx:responseError', function(evt) {
            const xhr = evt.detail.xhr;
            const status = xhr.status;

            if (status === 401) {
                // 未授权，重定向到登录页
                showError('未授权，请重新登录');
                setTimeout(() => {
                    window.location.href = '/admin/login';
                }, 1500);
            } else if (status === 403) {
                showError('无权限访问此资源');
            } else if (status === 404) {
                showError('请求的资源不存在');
            } else if (status >= 500) {
                showError('服务器错误，请稍后重试');
            } else {
                // 尝试解析错误消息
                try {
                    const response = JSON.parse(xhr.responseText);
                    showError(response.detail || '请求失败');
                } catch (e) {
                    showError('请求失败，请稍后重试');
                }
            }
        });
    }

    // ==================== 登录功能 ====================

    /**
     * 处理登录表单提交
     */
    function setupLoginForm() {
        const loginForm = document.getElementById('login-form');
        if (!loginForm) return;

        loginForm.addEventListener('submit', async function(e) {
            e.preventDefault();

            const username = loginForm.querySelector('[name="username"]').value;
            const password = loginForm.querySelector('[name="password"]').value;

            try {
                const response = await fetch('/admin/auth/login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ username, password })
                });

                const data = await response.json();

                if (response.ok) {
                    saveToken(data.token, data.expires_in);
                    showSuccess('登录成功，正在跳转...');
                    setTimeout(() => {
                        window.location.href = '/admin/';
                    }, 1000);
                } else {
                    showError(data.detail || '登录失败');
                }
            } catch (error) {
                showError('网络错误，请稍后重试');
            }
        });
    }

    // ==================== 页面加载初始化 ====================

    /**
     * 页面加载完成后执行初始化
     */
    function init() {
        // 设置 HTMX 认证头
        setupAuthHeaders();

        // 设置错误处理
        setupErrorHandler();

        // 设置登录表单（如果存在）
        setupLoginForm();

        // 检查 token 是否过期
        const token = getToken();
        const currentPath = window.location.pathname;

        if (!token && currentPath !== '/admin/login' && currentPath !== '/admin/') {
            // 未登录且不在登录页，重定向
            window.location.href = '/admin/login';
        }

        // 高亮当前导航项
        highlightCurrentNav();

        console.log('Admin WebUI 初始化完成');
    }

    /**
     * 高亮当前导航项
     */
    function highlightCurrentNav() {
        const currentPath = window.location.pathname;
        const navLinks = document.querySelectorAll('nav a');

        navLinks.forEach(link => {
            const href = link.getAttribute('href');
            if (href && currentPath.startsWith(href)) {
                link.classList.add('active');
            }
        });
    }

    // ==================== JSON 数据处理 ====================

    /**
     * 处理 HTMX JSON 响应，渲染密钥列表
     */
    function setupKeysListHandler() {
        const keysList = document.getElementById('keys-list');
        if (!keysList) return;

        document.body.addEventListener('htmx:afterRequest', function(evt) {
            if (evt.detail.target.id === 'keys-list') {
                try {
                    const response = JSON.parse(evt.detail.xhr.responseText);
                    renderKeysList(response);
                } catch (e) {
                    console.error('解析密钥数据失败:', e);
                }
            }
        });
    }

    /**
     * 渲染密钥列表
     */
    function renderKeysList(data) {
        const keysList = document.getElementById('keys-list');
        if (!keysList || !data.keys) return;

        const rows = data.keys.map(key => `
            <tr>
                <td>${key.provider}</td>
                <td>
                    <span class="key-status ${key.has_key ? 'active' : 'inactive'}">
                        ${key.has_key ? '已配置' : '未配置'}
                    </span>
                </td>
                <td>
                    <span class="key-masked">${key.key || 'N/A'}</span>
                </td>
                <td>
                    ${key.has_key ?
                        `<button class="btn btn-sm btn-danger" onclick="AdminWebUI.deleteKey('${key.provider}')">删除</button>
                        <button class="btn btn-sm btn-primary" onclick="AdminWebUI.rotateKey('${key.provider}')">轮换</button>` :
                        `<button class="btn btn-sm btn-success" onclick="AdminWebUI.createKey('${key.provider}')">新增</button>`
                    }
                </td>
            </tr>
        `).join('');

        keysList.innerHTML = rows || '<tr><td colspan="4" class="text-center">暂无数据</td></tr>';
    }

    /**
     * 处理监控数据渲染
     */
    function setupMonitoringHandlers() {
        const targets = ['circuit-breakers', 'rate-limits', 'health-status', 'dead-providers'];

        document.body.addEventListener('htmx:afterRequest', function(evt) {
            const targetId = evt.detail.target.id;
            if (targets.includes(targetId)) {
                try {
                    const response = JSON.parse(evt.detail.xhr.responseText);
                    renderMonitoringData(targetId, response);
                } catch (e) {
                    console.error(`解析${targetId}数据失败:`, e);
                }
            }
        });
    }

    /**
     * 渲染监控数据
     */
    function renderMonitoringData(targetId, data) {
        const target = document.getElementById(targetId);
        if (!target) return;

        let content = '';

        switch (targetId) {
            case 'circuit-breakers':
                if (data.circuit_breakers) {
                    content = data.circuit_breakers.map(cb => `
                        <div class="monitor-item">
                            <span class="provider-name">${cb.provider}</span>
                            <span class="status-indicator ${cb.state.toLowerCase()}"></span>
                            <span>${cb.state}</span>
                            <span class="failure-count">${cb.failure_count} failures</span>
                        </div>
                    `).join('');
                }
                break;

            case 'rate-limits':
                content = `
                    <div class="monitor-summary">
                        <span>总计 429: ${data.total_429 || 0}</span>
                        <span>最近24小时: ${data.last_24h?.length || 0} 次</span>
                    </div>
                `;
                break;

            case 'health-status':
                if (data.providers) {
                    content = data.providers.map(p => `
                        <div class="monitor-item">
                            <span class="provider-name">${p.provider}</span>
                            <span class="status-indicator ${p.alive ? 'closed' : 'open'}"></span>
                            <span>${p.alive ? '正常' : '异常'}</span>
                            ${p.consecutive_failures > 0 ? `<span class="failure-count">${p.consecutive_failures} failures</span>` : ''}
                        </div>
                    `).join('');
                }
                break;
        }

        target.innerHTML = content || '<div class="text-center">暂无数据</div>';
    }

    // ==================== 密钥操作功能 ====================

    /**
     * 删除密钥
     */
    async function deleteKey(provider) {
        if (!confirm(`确定要删除 ${provider} 的密钥吗？`)) return;

        try {
            const response = await fetch(`/admin/keys/${provider}`, {
                method: 'DELETE',
                headers: {
                    'Authorization': `Bearer ${getToken()}`,
                    'Content-Type': 'application/json'
                }
            });

            if (response.ok) {
                showSuccess('密钥删除成功');
                // 刷新列表
                htmx.trigger('#keys-list', 'load');
            } else {
                const data = await response.json();
                showError(data.detail || '删除失败');
            }
        } catch (error) {
            showError('网络错误，请稍后重试');
        }
    }

    /**
     * 轮换密钥
     */
    async function rotateKey(provider) {
        const newKey = prompt(`请输入 ${provider} 的新密钥：`);
        if (!newKey) return;

        try {
            const response = await fetch(`/admin/keys/${provider}/rotate`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${getToken()}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ new_key: newKey })
            });

            if (response.ok) {
                showSuccess('密钥轮换成功');
                htmx.trigger('#keys-list', 'load');
            } else {
                const data = await response.json();
                showError(data.detail || '轮换失败');
            }
        } catch (error) {
            showError('网络错误，请稍后重试');
        }
    }

    /**
     * 创建密钥
     */
    async function createKey(provider) {
        const key = prompt(`请输入 ${provider} 的密钥：`);
        if (!key) return;

        try {
            const response = await fetch('/admin/keys', {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${getToken()}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ provider, key })
            });

            if (response.ok) {
                showSuccess('密钥创建成功');
                htmx.trigger('#keys-list', 'load');
            } else {
                const data = await response.json();
                showError(data.detail || '创建失败');
            }
        } catch (error) {
            showError('网络错误，请稍后重试');
        }
    }

    // ==================== 备份功能 ====================

    /**
     * 处理备份导出
     */
    function setupBackupHandlers() {
        const exportForm = document.querySelector('form[hx-post*="/backup/export"]');
        if (exportForm) {
            exportForm.addEventListener('submit', function(e) {
                e.preventDefault();

                const includeSecrets = exportForm.querySelector('[name="include_secrets"]').checked;

                fetch('/admin/backup/export', {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${getToken()}`,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ include_secrets: includeSecrets })
                })
                .then(response => {
                    if (response.ok) {
                        // 获取文件名
                        const filename = response.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1] || 'backup.tar.gz';

                        // 下载文件
                        return response.blob().then(blob => ({ blob, filename }));
                    } else {
                        return response.json().then(data => {
                            throw new Error(data.detail || '导出失败');
                        });
                    }
                })
                .then(({ blob, filename }) => {
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = filename;
                    document.body.appendChild(a);
                    a.click();
                    window.URL.revokeObjectURL(url);
                    document.body.removeChild(a);
                    showSuccess('备份导出成功');
                })
                .catch(error => {
                    showError(error.message);
                });
            });
        }

        // 处理数据库大小显示
        document.body.addEventListener('htmx:afterRequest', function(evt) {
            if (evt.detail.target.id === 'db-sizes') {
                try {
                    const response = JSON.parse(evt.detail.xhr.responseText);
                    renderDbSizes(response);
                } catch (e) {
                    console.error('解析数据库大小数据失败:', e);
                }
            }
        });
    }

    /**
     * 渲染数据库大小信息
     */
    function renderDbSizes(data) {
        const target = document.getElementById('db-sizes');
        if (!target) return;

        const rows = Object.entries(data).map(([dbFile, size]) => {
            const sizeMB = (size / (1024 * 1024)).toFixed(2);
            const isLarge = size > 100 * 1024 * 1024; // 大于100MB

            return `
                <tr>
                    <td>${dbFile}</td>
                    <td class="${isLarge ? 'db-size-warning' : ''}">${sizeMB} MB</td>
                </tr>
            `;
        }).join('');

        target.innerHTML = `
            <table class="db-sizes-table">
                <thead>
                    <tr>
                        <th>数据库文件</th>
                        <th>大小</th>
                    </tr>
                </thead>
                <tbody>
                    ${rows || '<tr><td colspan="2" class="text-center">暂无数据</td></tr>'}
                </tbody>
            </table>
        `;
    }

    // ==================== Provider 管理 ====================

    /**
     * 编辑Provider
     */
    function editProvider(providerName) {
        window.location.href = `/admin/providers/${providerName}/edit`;
    }

    /**
     * 确认删除Provider
     */
    function confirmDeleteProvider(providerName) {
        if (confirm(`确定要删除Provider "${providerName}" 吗？此操作不可恢复！`)) {
            deleteProvider(providerName);
        }
    }

    /**
     * 删除Provider
     */
    async function deleteProvider(providerName) {
        try {
            const response = await fetch(`/api/admin/providers/${providerName}`, {
                method: 'DELETE',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${getToken()}`
                }
            });

            const result = await response.json();

            if (response.ok) {
                showSuccess(`Provider "${providerName}" 已删除`);
                // 刷新provider列表
                setTimeout(() => {
                    location.reload();
                }, 1000);
            } else {
                showError(`删除失败: ${result.detail || '未知错误'}`);
            }
        } catch (error) {
            showError(`请求错误: ${error.message}`);
        }
    }

    /**
     * 切换Provider状态
     */
    async function toggleProviderStatus(providerName, currentStatus) {
        const newStatus = currentStatus ? 0 : 1;
        const action = currentStatus ? '禁用' : '启用';

        try {
            const response = await fetch(`/api/admin/providers/${providerName}/config`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${getToken()}`
                },
                body: JSON.stringify({ is_active: newStatus })
            });

            const result = await response.json();

            if (response.ok) {
                showSuccess(`Provider "${providerName}" 已${action}`);
                // 刷新provider列表
                setTimeout(() => {
                    location.reload();
                }, 1000);
            } else {
                showError(`${action}失败: ${result.detail || '未知错误'}`);
            }
        } catch (error) {
            showError(`请求错误: ${error.message}`);
        }
    }

    // ==================== 页面加载初始化 ====================

    /**
     * 页面加载完成后执行初始化
     */
    function init() {
        // 设置 HTMX 认证头
        setupAuthHeaders();

        // 设置错误处理
        setupErrorHandler();

        // 设置登录表单（如果存在）
        setupLoginForm();

        // 设置密钥列表处理
        setupKeysListHandler();

        // 设置监控数据处理
        setupMonitoringHandlers();

        // 设置备份功能处理
        setupBackupHandlers();

        // 检查 token 是否过期
        const token = getToken();
        const currentPath = window.location.pathname;

        if (!token && currentPath !== '/admin/login' && currentPath !== '/admin/') {
            // 未登录且不在登录页，重定向
            window.location.href = '/admin/login';
        }

        // 高亮当前导航项
        highlightCurrentNav();

        console.log('Admin WebUI 初始化完成');
    }

    // ==================== 公开 API ====================

    // 暴露到全局作用域供页面使用
    window.AdminWebUI = {
        getToken,
        clearToken,
        showError,
        showSuccess,
        saveToken,
        deleteKey,
        rotateKey,
        createKey
    };

    // 暴露Provider管理函数到全局（供HTML直接调用）
    window.editProvider = editProvider;
    window.confirmDeleteProvider = confirmDeleteProvider;
    window.toggleProviderStatus = toggleProviderStatus;

    // DOM 加载完成后初始化
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
