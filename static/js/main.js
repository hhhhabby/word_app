// 文件选择显示
document.getElementById('file-input').addEventListener('change', function(e) {
    const fileName = e.target.files[0] ? e.target.files[0].name : '未选择文件';
    document.getElementById('file-name').textContent = fileName;
});

// 进度轮询变量
let progressInterval = null;
let startTime = null;

// 开始进度轮询
function startProgressPolling() {
    // 显示进度区域
    document.getElementById('progress-section').style.display = 'block';
    
    // 记录开始时间
    startTime = new Date();
    
    // 开始轮询
    progressInterval = setInterval(updateProgress, 500);
}

// 更新进度
function updateProgress() {
    fetch('/api/progress')
        .then(response => response.json())
        .then(data => {
            // 更新数字
            document.getElementById('total-words').textContent = data.total;
            document.getElementById('processed-words').textContent = data.processed;
            document.getElementById('remaining-words').textContent = data.remaining;
            
            // 更新进度条
            const percentage = data.total > 0 ? Math.round((data.processed / data.total) * 100) : 0;
            document.getElementById('progress-bar').style.width = percentage + '%';
            document.getElementById('progress-percentage').textContent = percentage + '%';
            
            // 更新处理时间
            if (data.is_processing) {
                const elapsed = Math.floor((new Date() - startTime) / 1000);
                document.getElementById('processing-time').textContent = elapsed + '秒';
            } else if (data.end_time) {
                // 处理完成
                clearInterval(progressInterval);
                const endTime = new Date(data.end_time);
                const totalSeconds = Math.floor((endTime - startTime) / 1000);
                document.getElementById('processing-time').textContent = totalSeconds + '秒';
            }
        })
        .catch(error => console.error('Progress error:', error));
}

// 表单提交
document.getElementById('upload-form').addEventListener('submit', function(e) {
    e.preventDefault();
    
    const formData = new FormData(this);
    const submitBtn = this.querySelector('button[type="submit"]');
    
    // 禁用按钮
    submitBtn.disabled = true;
    submitBtn.textContent = '处理中...';
    
    // 开始进度轮询
    startProgressPolling();
    
    fetch('/process', {
        method: 'POST',
        body: formData
    })
    .then(response => {
        if (response.ok) {
            return response.blob();
        }
        throw new Error('处理失败');
    })
    .then(blob => {
        // 下载文件
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = '带谐音助记.xlsx';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);
        
        // 恢复按钮
        submitBtn.disabled = false;
        submitBtn.textContent = '上传并生成';
    })
    .catch(error => {
        alert('处理失败：' + error.message);
        submitBtn.disabled = false;
        submitBtn.textContent = '上传并生成';
        clearInterval(progressInterval);
    });
});