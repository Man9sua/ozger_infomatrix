// ==================== CONFIGURATION ====================
let SUPABASE_URL = null;
let SUPABASE_ANON_KEY = null;
let AI_TEACHER_API_URL = null;
const DEFAULT_BACKEND_URL = window.location.origin;
const BACKEND_URL = window.BACKEND_URL || DEFAULT_BACKEND_URL;
const ENABLE_SOCKET = typeof window.ENABLE_SOCKET === 'boolean'
    ? window.ENABLE_SOCKET
    : /localhost|127\.0\.0\.1/.test(new URL(BACKEND_URL).hostname);

let supabaseClient = null;
let isAvatarSaving = false;
let isTestSaving = false;

const REQUEST_TIMEOUT_DEFAULT_MS = 4500;
const AI_UPLOAD_TIMEOUT_MS = 5 * 60 * 1000;
const AI_GENERATE_LEARN_TIMEOUT_MS = 10 * 60 * 1000;
const AI_GENERATE_QUIZ_TIMEOUT_MS = 6 * 60 * 1000;
const REQUEST_CACHE = new Map();
const REQUEST_IN_FLIGHT = new Map();

const IS_TOUCH_DEVICE = window.matchMedia('(hover: none), (pointer: coarse)').matches;
const PREFERS_REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const CPU_CORES = Number(navigator.hardwareConcurrency || 0);
const DEVICE_MEMORY_GB = Number(navigator.deviceMemory || 0);
const IS_LOW_POWER_DEVICE = (CPU_CORES > 0 && CPU_CORES <= 4) || (DEVICE_MEMORY_GB > 0 && DEVICE_MEMORY_GB <= 4);
const SHOULD_USE_LIGHT_EFFECTS = PREFERS_REDUCED_MOTION || IS_TOUCH_DEVICE || IS_LOW_POWER_DEVICE;
document.documentElement.classList.toggle('reduced-motion', SHOULD_USE_LIGHT_EFFECTS);

function debounce(fn, wait = 180) {
    let timeoutId = null;
    const wrapped = (...args) => {
        if (timeoutId) clearTimeout(timeoutId);
        timeoutId = setTimeout(() => fn(...args), wait);
    };
    wrapped.cancel = () => {
        if (timeoutId) {
            clearTimeout(timeoutId);
            timeoutId = null;
        }
    };
    return wrapped;
}

function getCachedRequest(cacheKey, ttlMs) {
    if (!cacheKey || ttlMs <= 0) return null;
    const cached = REQUEST_CACHE.get(cacheKey);
    if (!cached) return null;
    if ((Date.now() - cached.ts) > ttlMs) {
        REQUEST_CACHE.delete(cacheKey);
        return null;
    }
    return cached.data;
}

function setCachedRequest(cacheKey, data) {
    if (!cacheKey) return;
    REQUEST_CACHE.set(cacheKey, { ts: Date.now(), data });
}

async function fetchJsonWithControl(url, options = {}) {
    const {
        method = 'GET',
        headers = {},
        body,
        timeoutMs = REQUEST_TIMEOUT_DEFAULT_MS,
        cacheKey,
        cacheTtlMs = 0,
        signal,
        credentials = 'same-origin',
        mode = 'cors',
        cache = 'default',
        keepalive = false
    } = options;

    const upperMethod = String(method).toUpperCase();
    const key = cacheKey || `${upperMethod}:${url}`;

    if (upperMethod === 'GET') {
        const cached = getCachedRequest(key, cacheTtlMs);
        if (cached !== null) return cached;
        if (REQUEST_IN_FLIGHT.has(key)) return REQUEST_IN_FLIGHT.get(key);
    }

    const requestPromise = (async () => {
        const timeoutController = new AbortController();
        let didTimeout = false;
        const timeoutId = setTimeout(() => {
            didTimeout = true;
            timeoutController.abort();
        }, timeoutMs);
        const abortRelay = () => timeoutController.abort();

        if (signal) {
            if (signal.aborted) timeoutController.abort();
            else signal.addEventListener('abort', abortRelay, { once: true });
        }

        try {
            let response;
            try {
                response = await fetch(url, {
                    method: upperMethod,
                    headers,
                    body,
                    credentials,
                    mode,
                    cache,
                    keepalive,
                    signal: timeoutController.signal
                });
            } catch (error) {
                const rawMessage = String(error?.message || '').trim();
                const isAbort = error?.name === 'AbortError' || /abort|aborted/i.test(rawMessage);
                if (didTimeout && isAbort) {
                    throw new Error(`Request timeout after ${Math.max(1, Math.round(timeoutMs / 1000))}s`);
                }
                throw error;
            }

            const text = await response.text();
            let data = {};
            if (text) {
                try {
                    data = JSON.parse(text);
                } catch {
                    data = { message: text };
                }
            }

            if (!response.ok) {
                throw new Error(data?.error || data?.message || `HTTP ${response.status}: ${response.statusText}`);
            }

            if (upperMethod === 'GET' && cacheTtlMs > 0) {
                setCachedRequest(key, data);
            }

            return data;
        } finally {
            clearTimeout(timeoutId);
            if (signal) signal.removeEventListener('abort', abortRelay);
            if (upperMethod === 'GET') REQUEST_IN_FLIGHT.delete(key);
        }
    })();

    if (upperMethod === 'GET') {
        REQUEST_IN_FLIGHT.set(key, requestPromise);
    }

    return requestPromise;
}


// ==================== MATHJAX HELPERS ====================
function isMathJaxReady() {
    return typeof window !== 'undefined' &&
        window.MathJax &&
        typeof window.MathJax.typesetPromise === 'function';
}

async function typesetMathIn(element, attempt = 0) {
    if (!element) return;
    if (!isMathJaxReady()) {
        if (attempt < 6) {
            setTimeout(() => typesetMathIn(element, attempt + 1), 300);
        }
        return;
    }
    try {
        await window.MathJax.typesetPromise([element]);
    } catch (e) {
        console.warn('MathJax typeset failed:', e);
    }
}

function isMathLikeText(text) {
    if (!text) return false;
    const value = String(text);
    return /\\(|\\)|\\[|\\]|\$\$|\frac|\sqrt|\sin|\cos|\tan|\log|\ln|\pi|[0-9]\s*[+\-*/=^]|<=|>=|!=|[≤≥≠]/i.test(value);
}

function detectMathInValue(value) {
    if (!value) return false;
    if (Array.isArray(value)) return value.some(detectMathInValue);
    if (typeof value === 'object') return Object.values(value).some(detectMathInValue);
    return isMathLikeText(value);
}

function hasMathDelimiters(text) {
    if (!text) return false;
    return /\\(|\\)|\\[|\\]|\$\$|\$/.test(String(text));
}

function wrapMathIfLikely(text) {
    if (!text) return text;
    const value = String(text);
    if (hasMathDelimiters(value)) return value;
    const hasOperators = /[0-9]\s*[+\-*/=^]|<=|>=|!=|[\u2264\u2265\u2260]/.test(value);
    const hasTrig = /\b(sin|cos|tan|ctg|tg|cot|sec|csc|log|ln|pi|theta|arcsin|arccos|arctan)\b/i.test(value);
    const hasTexCommand = /\\(frac|sqrt|sum|int|sin|cos|tan|log|ln|pi|theta|alpha|beta|gamma|phi|rho|sigma)/i.test(value);
    const hasMathSymbols = /[\u221a\u2211\u222b\u03c0\u03b8]/.test(value);
    const looksShort = value.length <= 80;
    if ((hasOperators || hasTrig || hasTexCommand || hasMathSymbols) && looksShort) {
        return `\(${value}\)`;
    }
    return value;
}

function applyMathDisplayToLearnAnswers(container) {
    if (!container) return;
    container.querySelectorAll('.ai-learn-answer-btn').forEach(btn => {
        const raw = btn.textContent;
        const wrapped = wrapMathIfLikely(raw);
        if (wrapped !== raw) {
            btn.dataset.raw = raw;
            btn.textContent = wrapped;
        }
    });
}


async function initApp() {
    try {
        const config = await fetchJsonWithControl(`${BACKEND_URL}/api/config`, {
            timeoutMs: 3500,
            cacheKey: 'app-config',
            cacheTtlMs: 5 * 60 * 1000
        });
        SUPABASE_URL = config.supabaseUrl;
        SUPABASE_ANON_KEY = config.supabaseAnonKey;
        AI_TEACHER_API_URL = config.aiTeacherApiUrl || null;

        if (!AI_TEACHER_API_URL) {
            const isLocal = /localhost|127\.0\.0\.1/.test(new URL(BACKEND_URL).hostname);
            AI_TEACHER_API_URL = isLocal ? 'http://localhost:5000/api' : null;
        }

        window.AI_TEACHER_API_URL = AI_TEACHER_API_URL;
        
        if (typeof supabase !== 'undefined' && SUPABASE_URL && SUPABASE_ANON_KEY) {
            supabaseClient = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
                auth: {
                    storage: window.sessionStorage,
                    persistSession: true,
                    autoRefreshToken: true
                }
            });
            console.log('Supabase initialized successfully');
            
            
            
        } else {
            console.error('Supabase library not found or config missing');
        }
    } catch (error) {
        console.error('Failed to load configuration:', error);
    }
}


const appInitPromise = initApp();


let socket = null;

// ==================== STATE ====================
let currentUser = null;
let currentLang = 'kk'; 
let currentTheme = 'basic'; 
let pendingTheme = null; 
let userAvatar = null; 
let matrixAnimationId = null;
let matrixResizeHandler = null;
let matrixVisibilityHandler = null;


let factsData = [];
let currentModule = 0;
let currentCard = 0;
let score = 0;
let totalQuestions = 0;
let selectedMatchItem = null;
let matchedPairs = [];
let enabledModules = {
    flashcards: true,
    quiz: true,
    matching: true
};
let sectionScores = {
    flashcards: { correct: 0, total: 0, answered: 0 },
    quiz: { correct: 0, total: 0, answered: 0 },
    matching: { correct: 0, total: 0, answered: 0 }
};


let favorites = [];


let userLikes = [];


let userMaterials = [];
let userTests = [];
let currentLibraryTab = 'all';
let currentLibraryType = 'tests'; 
let currentSubjectFilter = 'all';
let currentVisibilityFilter = 'all';
let quicklookMaterial = null;
let deleteTargetId = null;


let userProfile = null;


let userStats = {
    totalTests: 0,
    guessStreak: 0,
    guessBestStreak: 0,
    entBestScore: 0,
    entTestsCompleted: 0
};

// ==================== LIMITS & USAGE TRACKING ====================
// Library test daily limit is disabled.
const DAILY_EXTERNAL_TEST_LIMIT = null;
const DAILY_EXTERNAL_TEST_WINDOW_MS = 24 * 60 * 60 * 1000;
const DAILY_EXTERNAL_TEST_STORAGE_KEY = 'ozger_external_test_history';

const AI_HOURLY_LIMIT = 5;
const AI_USAGE_WINDOW_MS = 60 * 60 * 1000;
const AI_USAGE_STORAGE_KEY = 'ozger_ai_usage_history';
const TEST_IMAGES_BUCKET = 'test-images';
const TEST_IMAGE_MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024;

function pruneTimestamps(timestamps, windowMs) {
    const now = Date.now();
    return (timestamps || []).filter(ts => typeof ts === 'number' && now - ts < windowMs);
}

function loadUsageHistory(storageKey, windowMs) {
    try {
        const raw = localStorage.getItem(storageKey);
        const parsed = raw ? JSON.parse(raw) : [];
        const cleaned = pruneTimestamps(parsed, windowMs);
        if (cleaned.length !== parsed.length) {
            localStorage.setItem(storageKey, JSON.stringify(cleaned));
        }
        return cleaned;
    } catch (error) {
        console.warn('Usage history load error:', error);
        return [];
    }
}

function saveUsageHistory(storageKey, history) {
    try {
        localStorage.setItem(storageKey, JSON.stringify(history || []));
    } catch (error) {
        console.warn('Usage history save error:', error);
    }
}

function getScopedStorageKey(baseKey) {
    const userId = currentUser?.id || 'guest';
    return `${baseKey}_${userId}`;
}

function getExternalTestUsageState() {
    return { used: 0, remaining: Number.POSITIVE_INFINITY, limit: null, unlimited: true };
}

function recordExternalTestCompletion() {
    // Disabled: no daily limit for library tests.
}

function getAIUsageState() {
    const history = loadUsageHistory(getScopedStorageKey(AI_USAGE_STORAGE_KEY), AI_USAGE_WINDOW_MS);
    const used = history.length;
    const remaining = Math.max(0, AI_HOURLY_LIMIT - used);
    return { used, remaining, limit: AI_HOURLY_LIMIT };
}

function recordAIUsage() {
    const storageKey = getScopedStorageKey(AI_USAGE_STORAGE_KEY);
    const history = loadUsageHistory(storageKey, AI_USAGE_WINDOW_MS);
    history.push(Date.now());
    saveUsageHistory(storageKey, history);
    updateAIUsageUI();
}

function formatDailyTestLimitText(remaining, limit) {
    return '';
}

function formatDailyLimitReachedText() {
    return '';
}

function formatAIUsageText(remaining, limit) {
    if (currentLang === 'kk') {
        return `AI пайдалану лимиті: ${remaining}/${limit} (осы сағатқа).`;
    }
    if (currentLang === 'ru') {
        return `Лимит AI: ${remaining}/${limit} (на этот час).`;
    }
    return `AI limit: ${remaining}/${limit} (this hour).`;
}

function updateDailyTestLimitUI() {
    const infoEl = document.getElementById('dailyTestLimitInfo');
    if (!infoEl) return;
    infoEl.textContent = '';
    infoEl.style.display = 'none';
}

function updateAIUsageUI() {
    const infoEl = document.getElementById('aiUsageRemaining');
    if (!infoEl) return;
    const { remaining, limit } = getAIUsageState();
    infoEl.textContent = formatAIUsageText(remaining, limit);
}

function canUseAIOrWarn() {
    const { remaining } = getAIUsageState();
    if (remaining <= 0) {
        showToast(formatAIUsageText(remaining, AI_HOURLY_LIMIT), 'warning');
        updateAIUsageUI();
        return false;
    }
    return true;
}


let regStep = 1;
let regData = {};


let currentSubject = null;
let currentMaterialForAction = null;
let currentEditorType = 'test';


let onlineUsers = [];

// ==================== TRANSLATIONS ====================
const i18n = {
    kk: {
        menu: 'Мәзір',
        changeStyle: 'Стиль ауыстыру',
        selectStyle: 'Стиль таңдаңыз',
        apply: 'Қолдану',
        styleChanged: 'Стиль сәтті өзгертілді!',
        changeLanguage: 'Тіл ауыстыру',
        selectLanguage: 'Тіл таңдаңыз',
        profile: 'Профиль',
        myMaterials: 'Менің материалдарым',
        favorites: 'Таңдаулылар',
        logout: 'Шығу',
        login: 'Кіру',
        register: 'Тіркелу',
        landingSubtitle: 'Оқуға көмектесетін платформа',
        startLearning: 'Бастау',
        chooseAction: 'Не істегіңіз келеді?',
        feature4Step: '4 Қадамдық оқыту',
        feature4StepDesc: 'Карточкалар, тест, сәйкестендіру арқылы үйрен',
        featureCreate: 'Материал жасау',
        featureCreateDesc: 'Өз тесттеріңді, карточкаларыңды жаса',
        featureLibrary: 'Кітапхана',
        featureLibraryDesc: 'Басқалардың материалдарын қара',
        featureFavorites: 'Таңдаулылар',
        featureFavoritesDesc: 'Сақталған материалдарың',
        inputTitle: 'Материалды енгізіңіз',
        correctFormat: 'Форматты түзету',
        loadSample: 'Мысал жүктеу',
        clear: 'Тазалау',
        formatHint: '📌 Формат:',
        formatHintText: 'Әрбір жолда нөмір, сұрақ және жауап қос нүктемен бөлінген.',
        startModules: 'Оқытуды бастау',
        selectModules: 'Оқыту модульдерін таңдаңыз',
        flashcards: 'Флэш-карталар',
        quiz: 'Тест',
        matching: 'Сәйкестендіру',
        cancel: 'Болдырмау',
        start: 'Бастау',
        correct: 'Дұрыс',
        total: 'Барлығы',
        accuracy: 'Дәлдік',
        prev: 'Алдыңғы',
        next: 'Келесі',
        prevModule: 'Алдыңғы модуль',
        nextModule: 'Келесі модуль',
        showResults: 'Нәтиже',
        congratulations: 'Құттықтаймыз!',
        completedAll: 'Сіз барлық модульдерді аяқтадыңыз!',
        library: 'Кітапхана',
        search: 'Іздеу...',
        noFavorites: 'Әзірге таңдаулылар жоқ',
        changeAvatar: 'Аватарды ауыстыру',
        username: 'Пайдаланушы аты:',
        emailPlaceholder: 'Email енгізіңіз',
        passwordPlaceholder: 'Құпия сөз',
        usernamePlaceholder: 'Пайдаланушы аты',
        confirmPassword: 'Құпия сөзді растаңыз',
        noAccount: 'Аккаунтыңыз жоқ па?',
        haveAccount: 'Аккаунтыңыз бар ма?',
        signUp: 'Тіркелу',
        signIn: 'Кіру',
        forgotPassword: 'Құпия сөзді ұмыттыңыз ба?',
        resetPassword: 'Электрондық поштаңызға кіру рұқсатын қалпына келтіру үшін сілтеме жібереміз.',
        sendResetLink: 'Сілтеме жіберу',
        loginSuccess: 'Сәтті кірдіңіз!',
        registerSuccess: 'Тіркелу сәтті! Email-ды тексеріңіз.',
        loginError: 'Кіру қатесі',
        registerError: 'Тіркелу қатесі',
        errorSavingTest: 'Тестті сақтау қатесі',
        logoutSuccess: 'Сәтті шықтыңыз',
        passwordMismatch: 'Құпия сөздер сәйкес келмейді',
        fillAllFields: 'Барлық өрістерді толтырыңыз',
        languageChanged: 'Тіл сәтті өзгертілді!',
        avatarUpdated: 'Аватар сәтті жаңартылды!',
        avatarSaved: 'Аватар Supabase-қа сақталды!',
        avatarSaveError: 'Аватарды сақтау қатесі',
        guest: 'Қонақ',
        pleaseLogin: 'Жалғастыру үшін кіріңіз немесе тіркеліңіз',
        faq1q: 'ozger дегеніміз не?',
        faq1a: 'ozger - ЕНТ-ға дайындық платформасы. AI Мұғалім көмегімен материалды үйреніп, тесттер өтуге болады.',
        faq2q: 'AI Мұғалім қалай жұмыс істейді?',
        faq2a: 'Мәтін немесе PDF жүктеп, Үйрену/Жаттығу/Real Test режимін таңдаңыз. AI сұрақтар мен түсіндірмелер жасайды.',
        faq3q: 'Өз тестімді қалай жасауға болады?',
        faq3a: 'Жүктеу бөлімінен "Тест құру" таңдап, сұрақтар мен жауаптарды енгізіңіз. Жалпыға немесе жеке етіп сақтауға болады.',
        faq4q: 'Real Test деген не?',
        faq4a: 'Real Test - нақты ЕНТ форматындағы тест. Көмексіз, жауапты өзгертуге болады, соңында толық нәтиже көрсетіледі.',
        errorEmpty: 'Материалды енгізіңіз',
        errorFormat: 'Материал форматы дұрыс емес',
        errorSelectModule: 'Кем дегенде бір модульді таңдаңыз',
        flashcardHint: '👆 Аудару үшін басыңыз',
        flashcardBackHint: '👆 Сұраққа қайту',
        flashcardKnew: '✓ Білдім',
        flashcardDidntKnow: '✗ Білмедім',
        allCardsDone: '🎉 Барлық карталар аяқталды!',
        goNextModule: 'Келесі модульге өтіңіз',
        quizQuestion: 'Сұрақ',
        matchingTitle: 'Сәйкестендіру',
        matchingQuestions: 'Сұрақтар',
        matchingAnswers: 'Жауаптар',
        allMatched: '🎉 Барлығы сәйкестендірілді!',
        resultsTitle: 'Нәтижелер',
        resultsBySection: '📊 Бөлімдер бойынша нәтижелер',
        correctWord: 'дұрыс',
        exitConfirm: 'Шығуды қалайсыз ба? Прогресс сақталмайды.',
        useMaterial: 'Қолдану',
        addToFavorites: 'Таңдаулыға қосу',
        removeFromFavorites: 'Таңдаулыдан алып тастау',
        addedToFavorites: 'Таңдаулыға қосылды',
        removedFromFavorites: 'Таңдаулыдан алынды',
        stars: 'жұлдыз',
        like: 'Ұнату',
        liked: 'Ұнатылды',
        unliked: 'Ұнату алынды',
        allMaterials: 'Барлығы',
        myUploads: 'Менікі',
        uploadMaterial: 'Жүктеу',
        materialTitle: 'Атауы',
        materialCategory: 'Санат',
        materialContent: 'Мазмұны',
        makePublic: 'Жалпыға қолжетімді',
        publish: 'Жариялау',
        titlePlaceholder: 'Мысалы: Биология негіздері',
        catOther: 'Басқа',
        catHistory: 'Тарих',
        catMath: 'Математика',
        catScience: 'Жаратылыстану',
        catLanguage: 'Тілдер',
        catGeography: 'География',
        preview: 'Алдын ала қарау',
        questions: 'сұрақ',
        starsLabel: 'жұлдыз',
        likes: 'лайк',
        startTest: 'Тестті бастау',
        untitled: 'Атаусыз',
        materialUploaded: 'Материал сәтті жүктелді!',
        fillTitleContent: 'Атау мен мазмұнды толтырыңыз',
        noMaterials: 'Материалдар табылмады',
        confirmDelete: 'Өшіруді растау',
        deleteConfirmText: 'Бұл материалды өшіргіңіз келе ме?',
        delete: 'Өшіру',
        materialDeleted: 'Материал өшірілді',
        andMore: 'және тағы',
        
        mainActions: 'Негізгі әрекеттер',
        libraryDesc: 'Материалдар мен тесттер',
        uploadDesc: 'Материал немесе тест жүктеу',
        favoritesDesc: 'Сақталған материалдар',
        historyKZ: 'Қазақстан тарихы',
        readingLit: 'Оқу сауаттылығы',
        mathLit: 'Мат. сауаттылық',
        profileSubject1: '1-ші профиль',
        profileSubject2: '2-ші профиль',
        mockENT: 'Пробный ЕНТ',
        materials: 'Материалдар',
        tests: 'Тесттер',
        allSubjects: 'Барлық пәндер',
        mySchool: 'Менің мектебім',
        myClass: 'Менің сыныбым',
        profileGroup: 'Профиль тобы',
        whatToUpload: 'Не жүктегіңіз келеді?',
        material: 'Материал',
        test: 'Тест',
        dailyTest: 'Күнделікті тест',
        dailyTestDesc: 'Кездейсоқ сұрақтар',
        topics: 'Тақырыптар',
        topicsDesc: 'Тақырып бойынша оқу',
        realTest: 'Нақты тест',
        realTestDesc: 'ЕНТ форматында тест',
        learn: 'Үйрену',
        practice: 'Жаттығу',
        siteGuide: 'Сайт нұсқаулығы',
        contact: 'Кері байланыс',
        country: 'Ел',
        city: 'Қала',
        school: 'Мектеп',
        class: 'Сынып',
        statistics: 'Статистика',
        testsCompleted: 'Тест өтілді',
        avgScore: 'Орташа балл',
        bestENT: 'Үздік ЕНТ',
        nextStep: 'Келесі',
        prevStep: 'Артқа',
        selectCountry: 'Елді таңдаңыз',
        selectCity: 'Қаланы таңдаңыз',
        selectSchool: 'Мектепті таңдаңыз',
        selectClass: 'Сыныпты таңдаңыз',
        classNumber: 'Сынып',
        classLetter: 'Әріп',
        title: 'Атауы',
        subject: 'Пән',
        content: 'Мазмұны',
        save: 'Сақтау',
        addQuestion: 'Сұрақ қосу',
        faq: 'Жиі қойылатын сұрақтар',
        guide1Title: '1. Тіркелу',
        guide1Text: 'Тіркеліп, профильдік пәндеріңізді (мысалы: Информатика-Математика) таңдаңыз.',
        guide2Title: '2. AI Мұғалім',
        guide2Text: 'Үйрену/Жаттығу/Real Test батырмасын басып, мәтін немесе PDF жүктеңіз. AI сұрақтар жасайды.',
        guide3Title: '3. Тест жасау',
        guide3Text: 'Жүктеу бөлімінен өз тестіңізді жасап, кітапханада бөлісіңіз.',
        guide4Title: '4. ЕНТ дайындық',
        guide4Text: 'ЕНТ секциясынан пәндер бойынша күнделікті тест, тақырыптар немесе Real Test өтіңіз.',
        profileUpdated: 'Профиль жаңартылды',
        classmates: 'Сыныптастар',
        students: 'оқушы',
        noClassmates: 'Сыныптастар табылмады',
        you: 'Сіз',
        
        deleteAvatar: 'Аватарды өшіру',
        avatarDeleted: 'Аватар сәтті өшірілді!',
        profileCombination: 'Профильдік комбинация',
        comboInformaticsMath: 'Информатика - Математика',
        comboGeographyMath: 'География - Математика',
        comboPhysicsMath: 'Физика - Математика',
        comboBiologyChemistry: 'Биология - Химия',
        comboBiologyGeography: 'Биология - География',
        comboHistoryEnglish: 'Дүниежүзі тарихы - Ағылшын',
        comboHistoryLaw: 'Дүниежүзі тарихы - Құқық',
        comboCreative: 'Шығармашылық',
        showPassword: 'Құпия сөзді көрсету',
        upload: 'Жүктеу',
        all: 'Барлығы',
        achievementUnlocked: 'Жетістік ашылды',
        
        achievementFirstTest: 'Бірінші тест',
        achievementFirstTestDesc: 'Бірінші тестті өту',
        achievementPerfectScore: 'Мінсіздік',
        achievementPerfectScoreDesc: '100% нәтиже алу',
        achievementExcellentScore: 'Үздік оқушы',
        achievementExcellentScoreDesc: '90%+ нәтиже алу',
        achievementMaterialCreator: 'Жасампаз',
        achievementMaterialCreatorDesc: 'Бірінші материал жасау',
        achievementSocialButterfly: 'Әлеуметтік',
        achievementSocialButterflyDesc: '5 сыныптаспен байланыс',
        achievementDedicatedLearner: 'Жігерлі оқушы',
        achievementDedicatedLearnerDesc: '10 тест өту',
        achievementEarlyBird: 'Ерте тұрушы',
        achievementEarlyBirdDesc: 'Таңертең оқу',
        achievementNightOwl: 'Ұяңышқы',
        achievementNightOwlDesc: 'Кешке оқу',
        
        inDevelopment: 'бә блмим шығар ма екен',
        
        guessGame: 'Тап',
        guessGameDesc: 'Тарихи тұлғаларды тап',
        guessIntroTitle: 'Тарихи тұлғаны тап!',
        guessIntroDesc: 'Фактілерді оқып, тарихи тұлғаны табуға тырысыңыз',
        currentStreak: 'Серия',
        bestStreak: 'Рекорд',
        startGame: 'Ойынды бастау',
        factLevel: 'Деңгей',
        enterAnswer: 'Жауапты енгізіңіз',
        check: 'Тексеру',
        wrongTryAgain: 'Қате! Келесі факт көрсетілді',
        skip: 'Өткізіп жіберу',
        nextQuestion: 'Келесі сұрақ',
        endGame: 'Аяқтау',
        correct: 'Дұрыс!',
        incorrect: 'Қате!',
        errorLoadingData: 'Деректерді жүктеу қатесі',
        learnModeInfo: 'Тесттерді үйрену режимі',
        practiceModeInfo: 'Жаттығу режимі',
        realTestModeInfo: 'Real Test режимі',
        
        selectMaterial: 'Материалды таңдаңыз',
        text: 'Мәтін',
        textDesc: 'Мәтінді қолмен енгізу',
        pdfDesc: 'PDF файлын жүктеу',
        enterMaterial: 'Материалды енгізіңіз',
        uploadPdf: 'PDF файлын жүктеңіз',
        dragPdf: 'PDF файлын сүйреңіз немесе',
        selectFile: 'Файлды таңдау',
        selectQuestionCount: 'Сұрақ санын таңдаңыз',
        aiGenerating: 'AI генерациялауда...',
        pleaseWait: 'Бұл біраз уақыт алуы мүмкін',
        learnMode: 'Оқу режимі',
        generalInfo: 'Жалпы ақпарат',
        summary: 'Конспект',
        timeline: 'Хронология',
        allQuestionsAnswered: 'Барлық сұрақтарға жауап берілді!',
        answerAllQuestions: 'Алдымен барлық сұрақтарға жауап беріңіз',
        finish: 'Аяқтау',
        congratulations: 'Құттықтаймыз!',
        completedMaterial: 'Сіз барлық материалды аяқтадыңыз!',
        practiceThisMaterial: 'Осы материалмен практика',
        clickToFlip: 'Карточканы аудару үшін басыңыз',
        didntKnow: 'Білмедім',
        knew: 'Білдім',
        practiceTest: 'Практика тест',
        question: 'Сұрақ',
        hint: 'Көмек',
        yourAnswer: 'Сіздің жауабыңыз',
        results: 'Нәтижелер',
        noAnswer: 'Жауап берілмеген',
        correctAnswer: 'Дұрыс жауап',
        repeat: 'Қайталау',
        continueOther: 'Басқа сұрақтармен жалғастыру',
        exit: 'Шығу',
        uploadError: 'Жүктеу қатесі',
        generationError: 'Генерация қатесі',
        noPlanFound: 'Оқу жоспары табылмады',
        selectPdf: 'PDF файлын таңдаңыз',
        continue: 'Жалғастыру',
        createTest: 'Тест құру',
        testName: 'Тест атауы',
        visibility: 'Көріну',
        publicTest: 'Жалпыға қолжетімді',
        privateTest: 'Жеке (тек маған)',
				questionOrder: 'Сұрақ реті',
				randomOrder: 'Кездейсоқ',
				staticOrder: 'Тұрақты',
        answerReveal: 'Жауаптарды көрсету',
        answersImmediately: 'Бірден',
        answersAtEnd: 'Соңында',
        enableHints: 'Көмек қосу',
        yes: 'Иә',
        no: 'Жоқ',
        publicTests: 'Жалпыға',
        privateTests: 'Жеке',
        saveTest: 'Тестті сақтау',
        enterQuestion: 'Сұрақты енгізіңіз',
        option: 'Нұсқа',
        enterTestName: 'Тест атауын енгізіңіз',
        addAtLeastOneQuestion: 'Кемінде бір сұрақ қосыңыз',
        testSaved: 'Тест сәтті сақталды!',
        testPublished: 'Тест жарияланды!',
        testPublishError: 'Тестті жариялау қатесі',
        loading: 'Жүктелуде',
        exitConfirmTitle: 'Шығуды растау',
        exitEditorWarning: 'Тест сақталмауы мүмкін. Шығуды қалайсыз ба?',
        stay: 'Қалу',
        exit: 'Шығу',
        markCorrect: 'Дұрыс жауап',
        markedAsCorrect: 'Дұрыс жауап ретінде белгіленді',
        cantDeleteCorrectAnswer: 'Дұрыс жауапты жоюға болмайды',
        minTwoOptions: 'Кем дегенде 2 нұсқа болуы керек',
        optionDeleted: 'Нұсқа жойылды',
        libraryRefreshed: 'Кітапхана жаңартылды',
        usernameEnglishOnly: 'Username тек ағылшын әріптері мен сандардан тұруы керек',
        usernameTaken: 'Бұл username бос емес, басқасын таңдаңыз',
        checkingUsername: 'Username тексерілуде...',
        usernameOrEmail: 'Email немесе username',
        userNotFound: 'Пайдаланушы табылмады',
        saveOrExitEditor: 'Алдымен тестті сақтаңыз немесе редактордан шығыңыз',
        testExitWarning: 'Тесттен шығасыз ба? Прогресс сақталмайды.',
        retry: 'Қайталау',
        profileSubject1Full: '1-ші профильдік пән',
        profileSubject2Full: '2-ші профильдік пән',
        basicInfo: 'Негізгі ақпарат',
        editQuestions: 'Сұрақтарды өңдеу',
        editorStep2Hint: 'Төмендегі блоктарды басып, сұрақ пен жауаптарды өңдеңіз. ✓ дұрыс жауапты белгілейді.',
        questionBlock: 'Сұрақ мәтіні',
        imageBlock: 'Сурет (міндетті емес)',
        answersBlock: 'Жауап нұсқалары',
        questionsNav: 'Сұрақтар бойынша навигация',
        clickToAddQuestion: 'Сұрақты жазу үшін басыңыз...',
        clickToAddAnswer: 'Жауапты жазу...',
        addImage: 'Сурет қосу (міндетті емес)',
        questionImageAlt: 'Сұрақ суреті',
        imageUrlPrompt: 'Сурет сілтемесін енгізіңіз (міндетті емес). Өшіру үшін бос қалдырыңыз.',
        invalidImageUrl: 'Сурет сілтемесі жарамсыз',
        invalidImageFile: 'Сурет файлын таңдаңыз',
        imageReadError: 'Сурет файлын оқу сәтсіз аяқталды',
        imageUploading: 'Сурет жүктелуде...',
        imageUploaded: 'Сурет сәтті жүктелді',
        imageUploadError: 'Суретті жүктеу сәтсіз аяқталды',
        imageTooLarge: 'Сурет тым үлкен (5MB дейін)',
        addOption: 'Нұсқа қосу',
        testNamePlaceholder: 'Мысалы: Қазақстан тарихы тесті',
        save: 'Сақтау',
        math: 'Математика',
        informatics: 'Информатика',
        physics: 'Физика',
        chemistry: 'Химия',
        biology: 'Биология',
        geography: 'География',
        worldHistory: 'Дүниежүзі тарихы',
        english: 'Ағылшын тілі',
        kazakhLit: 'Қазақ тілі мен әдебиеті',
        russianLit: 'Орыс тілі мен әдебиеті',
        law: 'Құқық негіздері',
        other: 'Басқа'
    },
    ru: {
        menu: 'Меню',
        changeStyle: 'Сменить стиль',
        selectStyle: 'Выберите стиль',
        apply: 'Применить',
        styleChanged: 'Стиль успешно изменён!',
        changeLanguage: 'Сменить язык',
        selectLanguage: 'Выберите язык',
        profile: 'Профиль',
        myMaterials: 'Мои материалы',
        favorites: 'Избранное',
        logout: 'Выйти',
        login: 'Вход',
        register: 'Регистрация',
        landingSubtitle: 'Платформа для обучения',
        startLearning: 'Начать',
        chooseAction: 'Что вы хотите сделать?',
        feature4Step: '4 Шаговое обучение',
        feature4StepDesc: 'Учись с карточками, тестами и сопоставлением',
        featureCreate: 'Создать материал',
        featureCreateDesc: 'Создавай свои тесты и карточки',
        featureLibrary: 'Библиотека',
        featureLibraryDesc: 'Смотри материалы других',
        featureFavorites: 'Избранное',
        featureFavoritesDesc: 'Твои сохранённые материалы',
        inputTitle: 'Введите материал',
        correctFormat: 'Исправить формат',
        loadSample: 'Загрузить пример',
        clear: 'Очистить',
        formatHint: '📌 Формат:',
        formatHintText: 'Каждая строка: номер, вопрос и ответ через двоеточие.',
        startModules: 'Начать обучение',
        selectModules: 'Выберите модули обучения',
        flashcards: 'Флэш-карты',
        quiz: 'Тест',
        matching: 'Сопоставление',
        cancel: 'Отмена',
        start: 'Начать',
        correct: 'Верно',
        total: 'Всего',
        accuracy: 'Точность',
        prev: 'Назад',
        next: 'Далее',
        prevModule: 'Предыдущий модуль',
        nextModule: 'Следующий модуль',
        showResults: 'Результат',
        congratulations: 'Поздравляем!',
        completedAll: 'Вы завершили все модули!',
        library: 'Библиотека',
        search: 'Поиск...',
        noFavorites: 'Пока нет избранного',
        changeAvatar: 'Сменить аватар',
        username: 'Имя пользователя:',
        emailPlaceholder: 'Введите email',
        passwordPlaceholder: 'Пароль',
        usernamePlaceholder: 'Имя пользователя',
        confirmPassword: 'Подтвердите пароль',
        noAccount: 'Нет аккаунта?',
        haveAccount: 'Есть аккаунт?',
        signUp: 'Зарегистрироваться',
        signIn: 'Войти',
        forgotPassword: 'Забыли пароль?',
        resetPassword: 'Мы отправим ссылку для восстановления доступа на ваш email',
        sendResetLink: 'Отправить ссылку',
        loginSuccess: 'Успешный вход!',
        registerSuccess: 'Регистрация успешна! Проверьте email.',
        loginError: 'Ошибка входа',
        registerError: 'Ошибка регистрации',
        errorSavingTest: 'Ошибка сохранения теста',
        logoutSuccess: 'Вы вышли из системы',
        passwordMismatch: 'Пароли не совпадают',
        fillAllFields: 'Заполните все поля',
        languageChanged: 'Язык успешно изменён!',
        avatarUpdated: 'Аватар успешно обновлён!',
        avatarSaved: 'Аватар сохранён в Supabase!',
        avatarSaveError: 'Ошибка сохранения аватара',
        guest: 'Гость',
        pleaseLogin: 'Войдите или зарегистрируйтесь чтобы продолжить',
        faq1q: 'Что такое ozger?',
        faq1a: 'ozger - платформа подготовки к ЕНТ. С помощью AI Учителя можно изучать материал и проходить тесты.',
        faq2q: 'Как работает AI Учитель?',
        faq2a: 'Загрузите текст или PDF, выберите режим Обучение/Практика/Real Test. AI создаст вопросы и объяснения.',
        faq3q: 'Как создать свой тест?',
        faq3a: 'В разделе Загрузка выберите "Создать тест", добавьте вопросы и ответы. Можно сделать публичным или приватным.',
        faq4q: 'Что такое Real Test?',
        faq4a: 'Real Test - тест в формате ЕНТ. Без подсказок, можно менять ответ, в конце показываются полные результаты.',
        errorEmpty: 'Введите материал',
        errorFormat: 'Неверный формат материала',
        errorSelectModule: 'Выберите хотя бы один модуль',
        flashcardHint: '👆 Нажмите, чтобы перевернуть',
        flashcardBackHint: '👆 Вернуться к вопросу',
        flashcardKnew: '✓ Знал',
        flashcardDidntKnow: '✗ Не знал',
        allCardsDone: '🎉 Все карточки завершены!',
        goNextModule: 'Переходите к следующему модулю',
        quizQuestion: 'Вопрос',
        matchingTitle: 'Сопоставление',
        matchingQuestions: 'Вопросы',
        matchingAnswers: 'Ответы',
        allMatched: '🎉 Всё сопоставлено!',
        resultsTitle: 'Результаты',
        resultsBySection: '📊 Результаты по разделам',
        correctWord: 'верно',
        exitConfirm: 'Выйти? Прогресс не сохранится.',
        useMaterial: 'Использовать',
        addToFavorites: 'В избранное',
        removeFromFavorites: 'Убрать из избранного',
        addedToFavorites: 'Добавлено в избранное',
        removedFromFavorites: 'Удалено из избранного',
        stars: 'звёзд',
        like: 'Нравится',
        liked: 'Лайк добавлен',
        unliked: 'Лайк убран',
        allMaterials: 'Все',
        myUploads: 'Мои',
        uploadMaterial: 'Загрузить',
        materialTitle: 'Название',
        materialCategory: 'Категория',
        materialContent: 'Содержание',
        makePublic: 'Доступен всем',
        publish: 'Опубликовать',
        titlePlaceholder: 'Например: Основы биологии',
        catOther: 'Другое',
        catHistory: 'История',
        catMath: 'Математика',
        catScience: 'Естествознание',
        catLanguage: 'Языки',
        catGeography: 'География',
        preview: 'Предпросмотр',
        questions: 'вопросов',
        starsLabel: 'звёзд',
        likes: 'лайков',
        startTest: 'Начать тест',
        untitled: 'Без названия',
        materialUploaded: 'Материал успешно загружен!',
        fillTitleContent: 'Заполните название и содержание',
        noMaterials: 'Материалы не найдены',
        confirmDelete: 'Подтверждение удаления',
        deleteConfirmText: 'Вы уверены, что хотите удалить этот материал?',
        delete: 'Удалить',
        materialDeleted: 'Материал удалён',
        andMore: 'и ещё',
        mainActions: 'Основные действия',
        libraryDesc: 'Материалы и тесты',
        uploadDesc: 'Загрузить материал или тест',
        favoritesDesc: 'Сохранённые материалы',
        historyKZ: 'История Казахстана',
        readingLit: 'Грамотность чтения',
        mathLit: 'Мат. грамотность',
        profileSubject1: '1-й профиль',
        profileSubject2: '2-й профиль',
        mockENT: 'Пробный ЕНТ',
        materials: 'Материалы',
        tests: 'Тесты',
        allSubjects: 'Все предметы',
        mySchool: 'Моя школа',
        myClass: 'Мой класс',
        profileGroup: 'Группа профиля',
        whatToUpload: 'Что загрузить?',
        material: 'Материал',
        test: 'Тест',
        dailyTest: 'Ежедневный тест',
        dailyTestDesc: 'Случайные вопросы',
        topics: 'Темы',
        topicsDesc: 'Обучение по темам',
        realTest: 'Реальный тест',
        realTestDesc: 'Тест в формате ЕНТ',
        learn: 'Учить',
        practice: 'Практика',
        siteGuide: 'Руководство',
        contact: 'Контакт',
        country: 'Страна',
        city: 'Город',
        school: 'Школа',
        class: 'Класс',
        statistics: 'Статистика',
        testsCompleted: 'Тестов пройдено',
        avgScore: 'Средний балл',
        bestENT: 'Лучший ЕНТ',
        nextStep: 'Далее',
        prevStep: 'Назад',
        selectCountry: 'Выберите страну',
        selectCity: 'Выберите город',
        selectSchool: 'Выберите школу',
        selectClass: 'Выберите класс',
        classNumber: 'Класс',
        classLetter: 'Буква',
        title: 'Название',
        subject: 'Предмет',
        content: 'Содержание',
        save: 'Сохранить',
        addQuestion: 'Добавить вопрос',
        faq: 'Часто задаваемые вопросы',
        guide1Title: '1. Регистрация',
        guide1Text: 'Зарегистрируйтесь и выберите профильные предметы (например: Информатика-Математика).',
        guide2Title: '2. AI Учитель',
        guide2Text: 'Нажмите Обучение/Практика/Real Test, загрузите текст или PDF. AI создаст вопросы.',
        guide3Title: '3. Создание теста',
        guide3Text: 'В разделе Загрузка создайте свой тест и поделитесь в библиотеке.',
        guide4Title: '4. Подготовка к ЕНТ',
        guide4Text: 'В секции ЕНТ проходите ежедневные тесты, темы или Real Test по предметам.',
        profileUpdated: 'Профиль обновлён',
        classmates: 'Одноклассники',
        students: 'учеников',
        noClassmates: 'Одноклассники не найдены',
        you: 'Вы',
        
        deleteAvatar: 'Удалить аватар',
        avatarDeleted: 'Аватар успешно удалён!',
        profileCombination: 'Профильная комбинация',
        comboInformaticsMath: 'Информатика - Математика',
        comboGeographyMath: 'География - Математика',
        comboPhysicsMath: 'Физика - Математика',
        comboBiologyChemistry: 'Биология - Химия',
        comboBiologyGeography: 'Биология - География',
        comboHistoryEnglish: 'Всемирная история - Английский',
        comboHistoryLaw: 'Всемирная история - Право',
        comboCreative: 'Творческий',
        showPassword: 'Показать пароль',
        upload: 'Загрузить',
        all: 'Все',
        achievementUnlocked: 'Достижение открыто',
        
        achievementFirstTest: 'Первый тест',
        achievementFirstTestDesc: 'Пройти первый тест',
        achievementPerfectScore: 'Идеально',
        achievementPerfectScoreDesc: 'Получить 100% результат',
        achievementExcellentScore: 'Отличник',
        achievementExcellentScoreDesc: 'Получить 90%+ результат',
        achievementMaterialCreator: 'Создатель',
        achievementMaterialCreatorDesc: 'Создать первый материал',
        achievementSocialButterfly: 'Социальный',
        achievementSocialButterflyDesc: 'Связаться с 5 одноклассниками',
        achievementDedicatedLearner: 'Усердный ученик',
        achievementDedicatedLearnerDesc: 'Пройти 10 тестов',
        achievementEarlyBird: 'Ранняя пташка',
        achievementEarlyBirdDesc: 'Учиться утром',
        achievementNightOwl: 'Сова',
        achievementNightOwlDesc: 'Учиться вечером',
        
        inDevelopment: 'Я хз будет ли',
        
        guessGame: 'Угадай',
        guessGameDesc: 'Угадай историческую личность',
        guessIntroTitle: 'Угадай историческую личность!',
        guessIntroDesc: 'Читайте факты и попробуйте угадать историческую личность',
        currentStreak: 'Серия',
        bestStreak: 'Рекорд',
        startGame: 'Начать игру',
        factLevel: 'Уровень',
        enterAnswer: 'Введите ответ',
        check: 'Проверить',
        wrongTryAgain: 'Неправильно! Показан следующий факт',
        skip: 'Пропустить',
        nextQuestion: 'Следующий вопрос',
        endGame: 'Завершить',
        correct: 'Правильно!',
        incorrect: 'Неправильно!',
        errorLoadingData: 'Ошибка загрузки данных',
        learnModeInfo: 'Режим изучения тестов',
        practiceModeInfo: 'Режим практики',
        realTestModeInfo: 'Режим Real Test',
        
        selectMaterial: 'Выберите материал',
        text: 'Текст',
        textDesc: 'Ввести текст вручную',
        pdfDesc: 'Загрузить PDF файл',
        enterMaterial: 'Введите материал',
        uploadPdf: 'Загрузите PDF файл',
        dragPdf: 'Перетащите PDF файл или',
        selectFile: 'Выбрать файл',
        selectQuestionCount: 'Выберите количество вопросов',
        aiGenerating: 'AI генерирует...',
        pleaseWait: 'Это может занять некоторое время',
        learnMode: 'Режим обучения',
        generalInfo: 'Общая информация',
        summary: 'Конспект',
        timeline: 'Хронология',
        allQuestionsAnswered: 'На все вопросы дан ответ!',
        answerAllQuestions: 'Сначала ответьте на все вопросы',
        finish: 'Завершить',
        congratulations: 'Поздравляем!',
        completedMaterial: 'Вы завершили весь материал!',
        practiceThisMaterial: 'Практика с этим материалом',
        clickToFlip: 'Нажмите, чтобы перевернуть карточку',
        didntKnow: 'Не знал',
        knew: 'Знал',
        practiceTest: 'Практика тест',
        question: 'Вопрос',
        hint: 'Подсказка',
        yourAnswer: 'Ваш ответ',
        results: 'Результаты',
        noAnswer: 'Ответ не дан',
        correctAnswer: 'Правильный ответ',
        repeat: 'Повторить',
        continueOther: 'Продолжить с другими вопросами',
        exit: 'Выход',
        uploadError: 'Ошибка загрузки',
        generationError: 'Ошибка генерации',
        noPlanFound: 'План обучения не найден',
        selectPdf: 'Выберите PDF файл',
        continue: 'Продолжить',
        createTest: 'Создать тест',
        testName: 'Название теста',
        visibility: 'Видимость',
        publicTest: 'Общедоступный',
        privateTest: 'Приватный (только для меня)',
				questionOrder: 'Порядок вопросов',
				randomOrder: 'Случайный',
				staticOrder: 'Статический',
        answerReveal: 'Показывать ответы',
        answersImmediately: 'Сразу',
        answersAtEnd: 'В конце',
        enableHints: 'Добавить подсказки',
        yes: 'Да',
        no: 'Нет',
        publicTests: 'Общие',
        privateTests: 'Личные',
        saveTest: 'Сохранить тест',
        enterQuestion: 'Введите вопрос',
        option: 'Вариант',
        enterTestName: 'Введите название теста',
        addAtLeastOneQuestion: 'Добавьте хотя бы один вопрос',
        testSaved: 'Тест успешно сохранён!',
        testPublished: 'Тест опубликован!',
        testPublishError: 'Ошибка публикации теста',
        loading: 'Загрузка',
        exitConfirmTitle: 'Подтвердить выход',
        exitEditorWarning: 'Тест может не сохраниться. Вы уверены, что хотите выйти?',
        stay: 'Остаться',
        exit: 'Выйти',
        markCorrect: 'Правильный ответ',
        markedAsCorrect: 'Отмечено как правильный ответ',
        cantDeleteCorrectAnswer: 'Нельзя удалить правильный ответ',
        minTwoOptions: 'Должно быть минимум 2 варианта',
        optionDeleted: 'Вариант удален',
        libraryRefreshed: 'Библиотека обновлена',
        usernameEnglishOnly: 'Username должен содержать только английские буквы и цифры',
        usernameTaken: 'Этот username уже занят, выберите другой',
        checkingUsername: 'Проверка username...',
        usernameOrEmail: 'Email или username',
        userNotFound: 'Пользователь не найден',
        saveOrExitEditor: 'Сначала сохраните тест или выйдите из редактора',
        testExitWarning: 'Выйти из теста? Прогресс не сохранится.',
        retry: 'Повторить',
        profileSubject1Full: '1-й профильный предмет',
        profileSubject2Full: '2-й профильный предмет',
        basicInfo: 'Основная информация',
        editQuestions: 'Редактировать вопросы',
        editorStep2Hint: 'Нажмите на блоки ниже, чтобы редактировать вопрос и ответы. ✓ отмечает правильный ответ.',
        questionBlock: 'Текст вопроса',
        imageBlock: 'Изображение (необязательно)',
        answersBlock: 'Варианты ответа',
        questionsNav: 'Навигация по вопросам',
        clickToAddQuestion: 'Нажмите, чтобы добавить вопрос...',
        clickToAddAnswer: 'Добавить ответ...',
        addImage: 'Добавить изображение (необязательно)',
        questionImageAlt: 'Изображение вопроса',
        imageUrlPrompt: 'Вставьте ссылку на изображение (необязательно). Оставьте пустым, чтобы удалить.',
        invalidImageUrl: 'Некорректная ссылка на изображение',
        invalidImageFile: 'Выберите файл изображения',
        imageReadError: 'Не удалось прочитать файл изображения',
        imageUploading: 'Загрузка изображения...',
        imageUploaded: 'Изображение успешно загружено',
        imageUploadError: 'Не удалось загрузить изображение',
        imageTooLarge: 'Изображение слишком большое (до 5MB)',
        addOption: 'Добавить вариант',
        testNamePlaceholder: 'Например: Тест по истории Казахстана',
        save: 'Сохранить',
        math: 'Математика',
        informatics: 'Информатика',
        physics: 'Физика',
        chemistry: 'Химия',
        biology: 'Биология',
        geography: 'География',
        worldHistory: 'Всемирная история',
        english: 'Английский язык',
        kazakhLit: 'Казахский язык и литература',
        russianLit: 'Русский язык и литература',
        law: 'Основы права',
        other: 'Другое'
    },
    en: {
        menu: 'Menu',
        changeStyle: 'Change Style',
        selectStyle: 'Select Style',
        apply: 'Apply',
        styleChanged: 'Style changed successfully!',
        changeLanguage: 'Change Language',
        selectLanguage: 'Select Language',
        profile: 'Profile',
        myMaterials: 'My Materials',
        favorites: 'Favorites',
        logout: 'Logout',
        login: 'Login',
        register: 'Register',
        landingSubtitle: 'Learning platform to help you study',
        startLearning: 'Start',
        chooseAction: 'What do you want to do?',
        feature4Step: '4 Step Learning',
        feature4StepDesc: 'Learn with flashcards, quizzes, and matching',
        featureCreate: 'Create Material',
        featureCreateDesc: 'Create your own tests and cards',
        featureLibrary: 'Library',
        featureLibraryDesc: 'Browse materials from others',
        featureFavorites: 'Favorites',
        featureFavoritesDesc: 'Your saved materials',
        inputTitle: 'Enter Material',
        correctFormat: 'Correct Format',
        loadSample: 'Load Sample',
        clear: 'Clear',
        formatHint: '📌 Format:',
        formatHintText: 'Each line: number, question and answer separated by colon.',
        startModules: 'Start Learning',
        selectModules: 'Select Learning Modules',
        flashcards: 'Flashcards',
        quiz: 'Quiz',
        matching: 'Matching',
        cancel: 'Cancel',
        start: 'Start',
        correct: 'Correct',
        total: 'Total',
        accuracy: 'Accuracy',
        prev: 'Previous',
        next: 'Next',
        prevModule: 'Previous Module',
        nextModule: 'Next Module',
        showResults: 'Results',
        congratulations: 'Congratulations!',
        completedAll: 'You completed all modules!',
        library: 'Library',
        search: 'Search...',
        noFavorites: 'No favorites yet',
        changeAvatar: 'Change Avatar',
        username: 'Username:',
        emailPlaceholder: 'Enter email',
        passwordPlaceholder: 'Password',
        usernamePlaceholder: 'Username',
        confirmPassword: 'Confirm password',
        noAccount: "Don't have an account?",
        haveAccount: 'Already have an account?',
        signUp: 'Sign Up',
        signIn: 'Sign In',
        forgotPassword: 'Forgot password?',
        resetPassword: 'We will send restore access link to your email',
        sendResetLink: 'Send link',
        loginSuccess: 'Login successful!',
        registerSuccess: 'Registration successful! Check your email.',
        loginError: 'Login error',
        registerError: 'Registration error',
        errorSavingTest: 'Error saving test',
        logoutSuccess: 'Logged out successfully',
        passwordMismatch: 'Passwords do not match',
        fillAllFields: 'Please fill all fields',
        languageChanged: 'Language changed successfully!',
        avatarUpdated: 'Avatar updated successfully!',
        avatarSaved: 'Avatar saved to Supabase!',
        avatarSaveError: 'Avatar save failed',
        guest: 'Guest',
        pleaseLogin: 'Please login or register to continue',
        faq1q: 'What is ozger?',
        faq1a: 'ozger is an ENT exam preparation platform. Use AI Teacher to learn material and take tests.',
        faq2q: 'How does AI Teacher work?',
        faq2a: 'Upload text or PDF, choose Learn/Practice/Real Test mode. AI will generate questions and explanations.',
        faq3q: 'How to create my own test?',
        faq3a: 'In Upload section select "Create Test", add questions and answers. You can make it public or private.',
        faq4q: 'What is Real Test?',
        faq4a: 'Real Test - test in ENT format. No hints, you can change answers, full results shown at the end.',
        errorEmpty: 'Please enter material',
        errorFormat: 'Invalid material format',
        errorSelectModule: 'Select at least one module',
        flashcardHint: '👆 Click to flip',
        flashcardBackHint: '👆 Return to question',
        flashcardKnew: '✓ Knew it',
        flashcardDidntKnow: '✗ Didn\'t know',
        allCardsDone: '🎉 All cards completed!',
        goNextModule: 'Proceed to next module',
        quizQuestion: 'Question',
        matchingTitle: 'Matching',
        matchingQuestions: 'Questions',
        matchingAnswers: 'Answers',
        allMatched: '🎉 All matched!',
        resultsTitle: 'Results',
        resultsBySection: '📊 Results by section',
        correctWord: 'correct',
        exitConfirm: 'Exit? Progress will not be saved.',
        useMaterial: 'Use',
        addToFavorites: 'Add to favorites',
        removeFromFavorites: 'Remove from favorites',
        addedToFavorites: 'Added to favorites',
        removedFromFavorites: 'Removed from favorites',
        stars: 'stars',
        like: 'Like',
        liked: 'Liked',
        unliked: 'Unliked',
        allMaterials: 'All',
        myUploads: 'My uploads',
        uploadMaterial: 'Upload',
        materialTitle: 'Title',
        materialCategory: 'Category',
        materialContent: 'Content',
        makePublic: 'Make public',
        publish: 'Publish',
        titlePlaceholder: 'e.g. Biology basics',
        catOther: 'Other',
        catHistory: 'History',
        catMath: 'Math',
        catScience: 'Science',
        catLanguage: 'Languages',
        catGeography: 'Geography',
        preview: 'Preview',
        questions: 'questions',
        starsLabel: 'stars',
        likes: 'likes',
        startTest: 'Start Test',
        untitled: 'Untitled',
        materialUploaded: 'Material uploaded successfully!',
        fillTitleContent: 'Please fill title and content',
        noMaterials: 'No materials found',
        confirmDelete: 'Confirm delete',
        deleteConfirmText: 'Are you sure you want to delete this material?',
        delete: 'Delete',
        materialDeleted: 'Material deleted',
        andMore: 'and more',
        mainActions: 'Main Actions',
        libraryDesc: 'Materials and tests',
        uploadDesc: 'Upload material or test',
        favoritesDesc: 'Saved materials',
        historyKZ: 'Kazakhstan History',
        readingLit: 'Reading Literacy',
        mathLit: 'Math Literacy',
        profileSubject1: '1st Profile',
        profileSubject2: '2nd Profile',
        mockENT: 'Mock ENT',
        materials: 'Materials',
        tests: 'Tests',
        allSubjects: 'All Subjects',
        mySchool: 'My School',
        myClass: 'My Class',
        profileGroup: 'Profile Group',
        whatToUpload: 'What to upload?',
        material: 'Material',
        test: 'Test',
        dailyTest: 'Daily Test',
        dailyTestDesc: 'Random questions',
        topics: 'Topics',
        topicsDesc: 'Learn by topic',
        realTest: 'Real Test',
        realTestDesc: 'ENT format test',
        learn: 'Learn',
        practice: 'Practice',
        siteGuide: 'Site Guide',
        contact: 'Contact',
        country: 'Country',
        city: 'City',
        school: 'School',
        class: 'Class',
        statistics: 'Statistics',
        testsCompleted: 'Tests Completed',
        avgScore: 'Avg Score',
        bestENT: 'Best ENT',
        nextStep: 'Next',
        prevStep: 'Back',
        selectCountry: 'Select country',
        selectCity: 'Select city',
        selectSchool: 'Select school',
        selectClass: 'Select class',
        classNumber: 'Grade',
        classLetter: 'Letter',
        title: 'Title',
        subject: 'Subject',
        content: 'Content',
        save: 'Save',
        addQuestion: 'Add Question',
        faq: 'Frequently Asked Questions',
        guide1Title: '1. Registration',
        guide1Text: 'Register and select your profile subjects (e.g., Informatics-Math).',
        guide2Title: '2. AI Teacher',
        guide2Text: 'Click Learn/Practice/Real Test, upload text or PDF. AI will generate questions.',
        guide3Title: '3. Create Tests',
        guide3Text: 'In Upload section create your own test and share in the library.',
        guide4Title: '4. ENT Preparation',
        guide4Text: 'In ENT section take daily tests, topics, or Real Test by subject.',
        profileUpdated: 'Profile updated',
        classmates: 'Classmates',
        students: 'students',
        noClassmates: 'No classmates found',
        you: 'You',
        
        deleteAvatar: 'Delete avatar',
        avatarDeleted: 'Avatar deleted successfully!',
        profileCombination: 'Profile combination',
        comboInformaticsMath: 'Informatics - Math',
        comboGeographyMath: 'Geography - Math',
        comboPhysicsMath: 'Physics - Math',
        comboBiologyChemistry: 'Biology - Chemistry',
        comboBiologyGeography: 'Biology - Geography',
        comboHistoryEnglish: 'World History - English',
        comboHistoryLaw: 'World History - Law',
        comboCreative: 'Creative',
        showPassword: 'Show password',
        upload: 'Upload',
        all: 'All',
        achievementUnlocked: 'Achievement unlocked',
        
        achievementFirstTest: 'First Test',
        achievementFirstTestDesc: 'Complete your first test',
        achievementPerfectScore: 'Perfect',
        achievementPerfectScoreDesc: 'Achieve 100% result',
        achievementExcellentScore: 'Excellent Student',
        achievementExcellentScoreDesc: 'Achieve 90%+ result',
        achievementMaterialCreator: 'Creator',
        achievementMaterialCreatorDesc: 'Create your first material',
        achievementSocialButterfly: 'Social',
        achievementSocialButterflyDesc: 'Connect with 5 classmates',
        achievementDedicatedLearner: 'Dedicated Learner',
        achievementDedicatedLearnerDesc: 'Complete 10 tests',
        achievementEarlyBird: 'Early Bird',
        achievementEarlyBirdDesc: 'Study in the morning',
        achievementNightOwl: 'Night Owl',
        achievementNightOwlDesc: 'Study at night',
        
        inDevelopment: 'Idk would it be released',
        
        guessGame: 'Guess',
        guessGameDesc: 'Guess the historical figure',
        guessIntroTitle: 'Guess the Historical Figure!',
        guessIntroDesc: 'Read the facts and try to guess the historical figure',
        currentStreak: 'Streak',
        bestStreak: 'Best',
        startGame: 'Start Game',
        factLevel: 'Level',
        enterAnswer: 'Enter your answer',
        check: 'Check',
        wrongTryAgain: 'Wrong! Next fact shown',
        skip: 'Skip',
        nextQuestion: 'Next question',
        endGame: 'End game',
        correct: 'Correct!',
        incorrect: 'Incorrect!',
        errorLoadingData: 'Error loading data',
        learnModeInfo: 'Learning mode for tests',
        practiceModeInfo: 'Practice mode',
        realTestModeInfo: 'Real Test mode',
        
        selectMaterial: 'Select Material',
        text: 'Text',
        textDesc: 'Enter text manually',
        pdfDesc: 'Upload PDF file',
        enterMaterial: 'Enter Material',
        uploadPdf: 'Upload PDF file',
        dragPdf: 'Drag PDF file or',
        selectFile: 'Select File',
        selectQuestionCount: 'Select question count',
        aiGenerating: 'AI generating...',
        pleaseWait: 'This may take some time',
        learnMode: 'Learning Mode',
        generalInfo: 'General Information',
        summary: 'Summary',
        timeline: 'Timeline',
        allQuestionsAnswered: 'All questions answered!',
        answerAllQuestions: 'Answer all questions first',
        finish: 'Finish',
        congratulations: 'Congratulations!',
        completedMaterial: 'You have completed all material!',
        practiceThisMaterial: 'Practice with this material',
        clickToFlip: 'Click to flip the card',
        didntKnow: "Didn't know",
        knew: 'Knew',
        practiceTest: 'Practice Test',
        question: 'Question',
        hint: 'Hint',
        yourAnswer: 'Your answer',
        results: 'Results',
        noAnswer: 'No answer',
        correctAnswer: 'Correct answer',
        repeat: 'Repeat',
        continueOther: 'Continue with other questions',
        exit: 'Exit',
        uploadError: 'Upload error',
        generationError: 'Generation error',
        noPlanFound: 'Learning plan not found',
        selectPdf: 'Select PDF file',
        continue: 'Continue',
        createTest: 'Create Test',
        testName: 'Test Name',
        visibility: 'Visibility',
        publicTest: 'Public',
        privateTest: 'Private (only for me)',
				questionOrder: 'Question Order',
				randomOrder: 'Random',
				staticOrder: 'Static',
        answerReveal: 'Show answers',
        answersImmediately: 'Immediately',
        answersAtEnd: 'At the end',
        enableHints: 'Enable hints',
        yes: 'Yes',
        no: 'No',
        publicTests: 'Public',
        privateTests: 'Private',
        saveTest: 'Save Test',
        enterQuestion: 'Enter question',
        option: 'Option',
        enterTestName: 'Enter test name',
        addAtLeastOneQuestion: 'Add at least one question',
        testSaved: 'Test saved successfully!',
        testPublished: 'Test published!',
        testPublishError: 'Test publish error',
        loading: 'Loading',
        exitConfirmTitle: 'Confirm Exit',
        exitEditorWarning: 'Test may not be saved. Are you sure you want to exit?',
        stay: 'Stay',
        exit: 'Exit',
        markCorrect: 'Correct Answer',
        markedAsCorrect: 'Marked as correct answer',
        cantDeleteCorrectAnswer: 'Cannot delete correct answer',
        minTwoOptions: 'Minimum 2 options required',
        optionDeleted: 'Option deleted',
        libraryRefreshed: 'Library refreshed',
        usernameEnglishOnly: 'Username must contain only English letters and numbers',
        usernameTaken: 'This username is already taken, please choose another',
        checkingUsername: 'Checking username...',
        usernameOrEmail: 'Email or username',
        userNotFound: 'User not found',
        saveOrExitEditor: 'Please save the test or exit the editor first',
        testExitWarning: 'Exit test? Progress will not be saved.',
        retry: 'Retry',
        profileSubject1Full: 'Profile Subject 1',
        profileSubject2Full: 'Profile Subject 2',
        basicInfo: 'Basic Information',
        editQuestions: 'Edit Questions',
        editorStep2Hint: 'Tap blocks below to edit the question and answers. ✓ marks the correct answer.',
        questionBlock: 'Question text',
        imageBlock: 'Image (optional)',
        answersBlock: 'Answer options',
        questionsNav: 'Question navigation',
        clickToAddQuestion: 'Click to add question...',
        clickToAddAnswer: 'Add answer...',
        addImage: 'Add image (optional)',
        questionImageAlt: 'Question image',
        imageUrlPrompt: 'Paste image URL (optional). Leave empty to remove.',
        invalidImageUrl: 'Invalid image URL',
        invalidImageFile: 'Please select an image file',
        imageReadError: 'Failed to read image file',
        imageUploading: 'Uploading image...',
        imageUploaded: 'Image uploaded successfully',
        imageUploadError: 'Failed to upload image',
        imageTooLarge: 'Image is too large (max 5MB)',
        addOption: 'Add option',
        testNamePlaceholder: 'E.g.: Kazakhstan History Test',
        save: 'Save',
        math: 'Mathematics',
        informatics: 'Computer Science',
        physics: 'Physics',
        chemistry: 'Chemistry',
        biology: 'Biology',
        geography: 'Geography',
        worldHistory: 'World History',
        english: 'English',
        kazakhLit: 'Kazakh Language and Literature',
        russianLit: 'Russian Language and Literature',
        law: 'Fundamentals of Law',
        other: 'Other'
    }
};

function t(key) {
    return (i18n[currentLang] && i18n[currentLang][key]) || (i18n['en'] && i18n['en'][key]) || key;
}


// ==================== WEBSOCKET FUNCTIONS ====================


async function initializeSocket() {
    if (socket) {
        socket.disconnect();
    }

    if (!ENABLE_SOCKET) {
        return;
    }

    if (!currentUser) {
        return;
    }

    try {
        
        const token = await getAuthToken();

        socket = io(BACKEND_URL, {
            auth: { token },
            transports: ['websocket', 'polling']
        });

        
        socket.on('connect', () => {
            console.log('Connected to server');
        });

        
        socket.on('online_users', (users) => {
            onlineUsers = users;
            updateOnlineUsersUI();
        });

        
        socket.on('user_online', (data) => {
            if (!onlineUsers.find(u => u.id === data.userId)) {
                onlineUsers.push({
                    id: data.userId,
                    username: data.username,
                    connectedAt: data.timestamp
                });
                updateOnlineUsersUI();
                showToast(`${data.username} онлайн`, 'info');
            }
        });

        
        socket.on('user_offline', (data) => {
            onlineUsers = onlineUsers.filter(u => u.id !== data.userId);
            updateOnlineUsersUI();
            showToast(`${data.username} оффлайн`, 'info');
        });

        
        socket.on('online_classmates', (classmates) => {
            updateClassmatesOnlineStatus(classmates);
        });

        
        socket.on('new_material', (data) => {
            showToast(`🆕 ${data.username} жаңа материал жүктеді: ${data.material.title}`, 'info');
            
            if (document.getElementById('libraryPage').classList.contains('hidden') === false) {
                supabaseTestsLoaded = false; 
                renderLibrary();
            }
        });

        
        socket.on('favorite_changed', (data) => {
            const heartIcon = data.isFavorited ? '❤️' : '🤍';
            showToast(`${heartIcon} ${data.username} материалды ${data.isFavorited ? 'таңдаулыға қосты' : 'таңдаулыдан алып тастады'}`, 'info');

            
            if (document.getElementById('favoritesPage').classList.contains('hidden') === false) {
                renderFavorites();
            }
        });

        
        socket.on('classmate_activity', (data) => {
            const activityMessages = {
                'test_completed': `✅ ${data.username} тестті аяқтады`,
                'material_viewed': `👁️ ${data.username} материалды қарап жатыр`,
                'material_created': `📝 ${data.username} жаңа материал жасады`,
                'favorite_added': `❤️ ${data.username} материалды таңдаулыға қосты`,
                'achievement_unlocked': `🏆 ${data.username} жаңа жетістікке қол жеткізді`
            };

            const message = activityMessages[data.activity] || `📌 ${data.username}: ${data.activity}`;
            showToast(message, 'info');

            
            addActivityItem({
                type: data.activity,
                message: message,
                timestamp: data.timestamp
            });
        });

        
        socket.on('announcement', (data) => {
            showToast(`📢 Мұғалім ${data.teacherName}: ${data.message}`, 'warning');
        });

        
        socket.on('achievement_notification', (data) => {
            showToast(`🏆 ${data.username} ${data.achievement} жетісігіне қол жеткізді!`, 'success');
        });

        
        socket.on('global_achievement', (data) => {
            showToast(`🌟 ${data.username} ${data.achievement} жетісігіне қол жеткізді!`, 'success');
        });

        
        socket.on('connect_error', (error) => {
            console.error('Socket connection error:', error);
            showToast('Сервермен байланыс үзілді', 'error');
        });

        
        socket.on('disconnect', () => {
            console.log('Disconnected from server');
            onlineUsers = [];
            updateOnlineUsersUI();
        });

    } catch (error) {
        console.error('Failed to initialize socket:', error);
    }
}


function disconnectSocket() {
    if (socket) {
        socket.disconnect();
        socket = null;
        onlineUsers = [];
        updateOnlineUsersUI();
    }
}


function updateOnlineUsersUI() {

    const classmatesList = document.getElementById('classmatesList');
    if (classmatesList && document.getElementById('classmatesPage').classList.contains('hidden') === false) {
        
        if (socket && socket.connected) {
            socket.emit('get_online_classmates');
        }
    }
}


function updateClassmatesOnlineStatus(onlineClassmates) {
    const classmatesList = document.getElementById('classmatesList');
    if (!classmatesList) return;

    const classmateItems = classmatesList.querySelectorAll('.classmate-item');
    classmateItems.forEach(item => {
        const username = item.querySelector('.classmate-name')?.textContent;
        const onlineIndicator = item.querySelector('.online-indicator');

        if (username) {
            const isOnline = onlineClassmates.some(c => c.username === username);
            if (isOnline) {
                if (!onlineIndicator) {
                    const indicator = document.createElement('div');
                    indicator.className = 'online-indicator';
                    indicator.innerHTML = '🟢';
                    indicator.title = 'Онлайн';
                    item.appendChild(indicator);
                }
            } else {
                if (onlineIndicator) {
                    onlineIndicator.remove();
                }
            }
        }
    });
}


function joinMaterialsRoom() {
    if (socket && socket.connected) {
        socket.emit('join_materials');
    }
}

function leaveMaterialsRoom() {
    if (socket && socket.connected) {
        socket.emit('leave_materials');
    }
}


function joinFavoritesRoom() {
    if (socket && socket.connected) {
        socket.emit('join_favorites');
    }
}

function leaveFavoritesRoom() {
    if (socket && socket.connected) {
        socket.emit('leave_favorites');
    }
}


function joinClassroom() {
    if (socket && socket.connected && userProfile && userProfile.school && userProfile.class_number) {
        const classroomId = `${userProfile.school}_${userProfile.class_number}`;
        socket.emit('join_classroom', classroomId);
    }
}


function leaveClassroom() {
    if (socket && socket.connected && userProfile && userProfile.school && userProfile.class_number) {
        const classroomId = `${userProfile.school}_${userProfile.class_number}`;
        socket.emit('leave_classroom', classroomId);
    }
}


function sendUserActivity(activity, details = {}) {
    if (socket && socket.connected) {
        socket.emit('user_activity', { activity, details });
    }
}


function sendClassroomAnnouncement(message) {
    if (socket && socket.connected) {
        socket.emit('classroom_announcement', { message });
    }
}


function sendAchievementUnlocked(achievement, description) {
    if (socket && socket.connected) {
        socket.emit('achievement_unlocked', { achievement, description });
    }
}


function getCookie(name) {
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(';').shift();
}

async function getAuthToken() {
    
    if (!supabaseClient && typeof appInitPromise?.then === 'function') {
        try {
            await appInitPromise;
        } catch (err) {
            console.warn('getAuthToken: init error', err);
        }
    }
    if (supabaseClient?.auth?.getSession) {
        try {
            const { data, error } = await supabaseClient.auth.getSession();
            if (!error && data?.session?.access_token) {
                return data.session.access_token;
            }
        } catch (err) {
            console.warn('getAuthToken: supabase session error', err);
        }
    }
    return getCookie('supabase_auth_token') || '';
}

async function ensureSupabaseReady() {
    if (supabaseClient) return true;
    if (typeof appInitPromise?.then === 'function') {
        try {
            await appInitPromise;
        } catch (err) {
            console.warn('ensureSupabaseReady: init error', err);
        }
    }
    if (!supabaseClient) {
        try {
            await initApp();
        } catch (err) {
            console.warn('ensureSupabaseReady: retry init error', err);
        }
    }
    return !!supabaseClient;
}

async function ensureSessionLoaded() {
    if (currentUser) return currentUser;
    const ready = await ensureSupabaseReady();
    if (!ready) return null;
    await loadSession();
    return currentUser;
}

// ==================== SAMPLE DATA ====================
const sampleMaterial = `1. Қазақ хандығының негізін қалаған: Керей мен Жәнібек
2. Қазақ хандығы құрылған жыл: 1465 жыл
3. Алтын Орда ыдыраған соң қалыптасқан хандық: Ақ Орда
4. Қазақ халқының ата-бабалары: Сақтар, Ғұндар, Түріктер
5. "Қазақ" сөзінің мағынасы: Еркін адам
6. Әбілқайыр хан билеген: Өзбек хандығын
7. Тәуке хан қабылдаған заңдар: "Жеті жарғы"
8. Қазақ жүздерінің саны: Үш жүз
9. Ұлы жүзді басқарған би: Төле би
10. Орта жүзді басқарған би: Қазыбек би`;

const libraryMaterials = [
    {
        id: 1,
        title: 'Қазақстан тарихы',
        author: 'Әкімше',
        count: 10,
        category: 'history',
        content: sampleMaterial
    }
];

// ==================== TOAST NOTIFICATIONS ====================
function showToast(message, type = 'info', duration = 5000) {
    const container = document.getElementById('toastContainer');
    if (!container) return;
    
    const icons = {
        success: '✓',
        error: '✗',
        warning: '⚠',
        info: 'ℹ'
    };
    
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;

    const toastContent = document.createElement('div');
    toastContent.className = 'toast-content';

    const toastIcon = document.createElement('span');
    toastIcon.className = 'toast-icon';
    toastIcon.textContent = icons[type] || icons.info;

    const toastMessage = document.createElement('span');
    toastMessage.className = 'toast-message';
    toastMessage.textContent = message;

    const toastProgress = document.createElement('div');
    toastProgress.className = 'toast-progress';

    toastContent.appendChild(toastIcon);
    toastContent.appendChild(toastMessage);
    toast.appendChild(toastContent);
    toast.appendChild(toastProgress);
    
    container.appendChild(toast);
    
    setTimeout(() => {
        toast.classList.add('toast-out');
        setTimeout(() => toast.remove(), 300);
    }, duration);
    
    toast.addEventListener('click', () => {
        toast.classList.add('toast-out');
        setTimeout(() => toast.remove(), 300);
    });
}

// ==================== NAVIGATION ====================
function hideAllPages() {
    
    if (!document.getElementById('libraryPage')?.classList.contains('hidden')) {
        leaveMaterialsRoom();
    }

    
    if (!document.getElementById('favoritesPage')?.classList.contains('hidden')) {
        leaveFavoritesRoom();
    }

    document.getElementById('landingPage')?.classList.add('hidden');
    document.getElementById('homePage')?.classList.add('hidden');
    document.getElementById('inputPage')?.classList.add('hidden');
    document.getElementById('learningPage')?.classList.add('hidden');
    document.getElementById('libraryPage')?.classList.add('hidden');
    document.getElementById('favoritesPage')?.classList.add('hidden');
    document.getElementById('guessGamePage')?.classList.add('hidden');
    document.getElementById('profilePage')?.classList.add('hidden');
    document.getElementById('classmatesPage')?.classList.add('hidden');
    document.getElementById('testEditorPage')?.classList.add('hidden');
    document.getElementById('testTakingPage')?.classList.add('hidden');
    
    
    document.getElementById('aiLearnPage')?.classList.add('hidden');
    document.getElementById('aiLearnCompletePage')?.classList.add('hidden');
    document.getElementById('aiFlashcardsPage')?.classList.add('hidden');
    document.getElementById('aiTestPage')?.classList.add('hidden');
    document.getElementById('aiResultsPage')?.classList.add('hidden');

    closeAllSidePanels();
    closeAllModals();
    if (typeof closeEditOverlay === 'function') {
        closeEditOverlay();
    }
    if (AITeacher && !AITeacher._inFlight) {
        hideAILoading();
    }
}

function showLanding() {
    
    if (isInTestEditorPage()) {
        showToast(t('saveOrExitEditor'), 'warning');
        return;
    }
    hideAllPages();
    document.getElementById('landingPage')?.classList.remove('hidden');
}

function showHome(bypassEditorCheck = false) {
    
    if (!currentUser) {
        showLanding();
        return;
    }
    
    
    if (!bypassEditorCheck && isInTestEditorPage()) {
        showToast(t('saveOrExitEditor'), 'warning');
        return;
    }
    hideAllPages();
    document.getElementById('homePage')?.classList.remove('hidden');
    updateENTProfileSubjects();

    
    updateClassroomInfo();
}

function showInputSection() {
    hideAllPages();
    document.getElementById('inputPage')?.classList.remove('hidden');
}

function showCreateSection() {
    showInputSection();
}

function showLearning() {
    hideAllPages();
    document.getElementById('learningPage')?.classList.remove('hidden');
}

async function showLibrary() {
    hideAllPages();
    document.getElementById('libraryPage')?.classList.remove('hidden');

    updateDailyTestLimitUI();
    
    
    const visibilityTabs = document.querySelector('.library-filters .filter-tabs:last-child');
    if (visibilityTabs) {
        visibilityTabs.style.display = currentLibraryTab === 'my' ? 'flex' : 'none';
    }
    
    
    await renderLibrary();
    
    
    joinMaterialsRoom();
}



function showFavorites() {
    hideAllPages();
    document.getElementById('favoritesPage')?.classList.remove('hidden');
    renderFavorites();
}


function joinLibraryRooms() {
    joinMaterialsRoom();
}


function joinFavoritesRoom() {
    
    if (socket && socket.connected) {
        socket.emit('join_favorites', { user_id: currentUser?.id });
    }
}

function showProfile() {
    hideAllPages();
    document.getElementById('profilePage')?.classList.remove('hidden');
    renderProfilePage();
    
}

function showClassmates() {
    hideAllPages();
    document.getElementById('classmatesPage')?.classList.remove('hidden');
    renderClassmates();

    
    if (socket && socket.connected) {
        socket.emit('get_online_classmates');
    }
}

// ==================== CLASSROOM ====================
function updateClassroomInfo() {
    const classroomSection = document.getElementById('classroomSection');
    const onlineCount = document.getElementById('onlineClassmatesCount');

    if (userProfile && userProfile.school && userProfile.class_number) {
        
        if (classroomSection) {
            classroomSection.style.display = 'block';
        }

        
        if (onlineCount) {
            const onlineUsersInClass = onlineUsers.filter(user => {
                
                
                return true;
            });
            onlineCount.textContent = onlineUsersInClass.length;
        }
    } else {
        
        if (classroomSection) {
            classroomSection.style.display = 'none';
        }
    }
}

function addActivityItem(activity) {
    const activityList = document.getElementById('activityList');
    if (!activityList) return;

    const activityItem = document.createElement('div');
    activityItem.className = 'activity-item';

    const icon = document.createElement('div');
    icon.className = 'activity-icon';

    const content = document.createElement('div');
    content.className = 'activity-content';
    content.textContent = activity.message;

    const time = document.createElement('div');
    time.className = 'activity-time';
    time.textContent = new Date(activity.timestamp).toLocaleTimeString();

    
    switch (activity.type) {
        case 'test_completed':
            icon.textContent = '✅';
            break;
        case 'material_created':
            icon.textContent = '📝';
            break;
        case 'favorite_added':
            icon.textContent = '❤️';
            break;
        case 'achievement_unlocked':
            icon.textContent = '🏆';
            break;
        default:
            icon.textContent = '📌';
    }

    activityItem.appendChild(icon);
    activityItem.appendChild(content);
    activityItem.appendChild(time);

    
    activityList.insertBefore(activityItem, activityList.firstChild);

    
    while (activityList.children.length > 10) {
        activityList.removeChild(activityList.lastChild);
    }
}

// ==================== CLASSMATES ====================


function getSubjectNames() {
    
    return {
        math: t('math') || 'Математика',
        physics: t('physics') || 'Физика',
        chemistry: t('chemistry') || 'Химия',
        biology: t('biology') || 'Биология',
        geography: t('geography') || 'География',
        world_history: t('worldHistory') || 'Дүниежүзі тарихы',
        english: t('english') || 'Ағылшын тілі',
        informatics: t('informatics') || 'Информатика',
        law: t('law') || 'Құқық негіздері'
    };
}

function showUploadModal() {
    startUpload('test');
}

function startUpload(type) {
    currentEditorType = 'test';
    showTestEditorPage();
}

async function saveMaterial() {
    if (!currentUser) {
        showToast(t('pleaseLogin'), 'warning');
        return;
    }

    const title = document.getElementById('editorTitle')?.value?.trim();
    const content = document.getElementById('materialInput')?.value?.trim();
    const subject = document.getElementById('editorSubject')?.value;
    const isPublic = document.getElementById('editorPublic')?.checked;

    if (!title || !content) {
        showToast(t('fillTitleContent'), 'warning');
        return;
    }

    try {
        const token = await getAuthToken();
        const result = await fetchJsonWithControl(`${BACKEND_URL}/api/materials`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify({
                title,
                content,
                subject,
                type: currentEditorType,
                is_public: isPublic
            }),
            timeoutMs: 6000
        });

        showToast(t('materialUploaded'), 'success');

        
        sendUserActivity('material_created', {
            title: title,
            subject: subject,
            type: currentEditorType
        });

        
        unlockAchievement('material_creator');

        
        document.getElementById('editorTitle').value = '';
        document.getElementById('materialInput').value = '';
        document.getElementById('editorSubject').selectedIndex = 0;
        document.getElementById('editorPublic').checked = true;

        
        showLibrary();

    } catch (error) {
        console.error('Error saving material:', error);
        showToast('Материалды сақтау қатесі', 'error');
    }
}

/* async function toggleFavorite(materialId) {
    if (!currentUser) {
        showToast(t('pleaseLogin'), 'warning');
        return false;
    }

    try {
        const token = await getAuthToken();
        const response = await fetch(`${BACKEND_URL}/api/favorites`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
        const result = await fetchJsonWithControl(`${BACKEND_URL}/api/favorites`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify({ material_id: materialId }),
            timeoutMs: 5000
        });
            isFavorited: isFavorited
        });

        return isFavorited;
    } catch (error) {
        console.error('Error toggling favorite:', error);
        showToast('Таңдаулыны өзгерту қатесі', 'error');
        return false;
    }
} */

async function toggleFavorite(materialId, isUserMaterial = false) {
    if (!currentUser) {
        await ensureSessionLoaded();
    }
    if (!currentUser) {
        showToast(t('pleaseLogin'), 'warning');
        return false;
    }

    const favoriteId = isUserMaterial ? `user_${materialId}` : materialId;
    const index = favorites.indexOf(favoriteId);
    const wasInFavorites = index > -1;
    const isNowFavorite = !wasInFavorites;

    if (wasInFavorites) {
        favorites.splice(index, 1);
    } else {
        favorites.push(favoriteId);
    }

    renderLibrary();
    renderFavorites();

    try {
        const syncOk = isNowFavorite
            ? await addToFavoritesSupabase(favoriteId)
            : await removeFromFavoritesSupabase(favoriteId);

        if (!syncOk) {
            console.warn('Favorite sync finished with non-success result');
        }
    } catch (error) {
        console.error('Error syncing favorite:', error);
    }

    return isNowFavorite;
}

// ==================== PROFILE PAGE ====================
function renderProfilePage() {
    if (!currentUser && !userProfile) return;
    
    const profile = userProfile || {};
    const user = currentUser || {};
    
    document.getElementById('profilePageUsername').textContent = 
        profile.username || user.user_metadata?.username || t('guest');
    document.getElementById('profilePageEmail').textContent = 
        user.email || profile.email || '-';
    document.getElementById('profilePageCountry').textContent = 
        profile.country || 'Қазақстан';
    document.getElementById('profilePageCity').textContent = 
        profile.city || '-';
    document.getElementById('profilePageSchool').textContent = 
        profile.school || '-';
    
    
    const classNumberSelect = document.getElementById('profilePageClassNumber');
    const classLetterSelect = document.getElementById('profilePageClassLetter');
    
    if (classNumberSelect && profile.classNumber) {
        classNumberSelect.value = profile.classNumber;
    } else if (classNumberSelect && profile.class) {
        
        classNumberSelect.value = profile.class.substring(0, 2);
    }
    
    if (classLetterSelect && profile.classLetter) {
        classLetterSelect.value = profile.classLetter;
    } else if (classLetterSelect && profile.class) {
        
        classLetterSelect.value = profile.class.substring(2);
    }
    
    const comboSelect = document.getElementById('profilePageSubjectCombination');
    if (comboSelect && profile.subjectCombination) {
        comboSelect.value = profile.subjectCombination;
    }
    
    
    updateENTProfileSubjects();
    
    
    if (userAvatar) {
        const placeholder = document.getElementById('pageAvatarPlaceholder');
        const img = document.getElementById('pageAvatarImg');
        if (placeholder && img) {
            placeholder.classList.add('hidden');
            img.classList.remove('hidden');
            img.src = userAvatar;
        }
    }
}

function updateProfileField(field) {
    if (!userProfile) userProfile = {};
    
    const value = document.getElementById(`profilePage${capitalize(field)}`)?.value;
    if (value) {
        userProfile[field] = value;
        
        if (currentUser) saveProfileToSupabase(currentUser.id, userProfile);
        showToast(t('profileUpdated'), 'success');
        updateENTProfileSubjects();
    }
}

function updateProfileCombination() {
    if (!userProfile) userProfile = {};
    
    const combo = document.getElementById('profilePageSubjectCombination')?.value;
    if (combo) {
        userProfile.subjectCombination = combo;
        const parsed = parseSubjectCombination(combo);
        userProfile.subject1 = parsed.subject1;
        userProfile.subject2 = parsed.subject2;
        
        if (currentUser) saveProfileToSupabase(currentUser.id, userProfile);
        showToast(t('profileUpdated'), 'success');
        updateENTProfileSubjects();
    }
}

function parseSubjectCombination(combo) {
    const combinations = {
        'informatics-math': { subject1: 'informatics', subject2: 'math' },
        'geography-math': { subject1: 'geography', subject2: 'math' },
        'physics-math': { subject1: 'physics', subject2: 'math' },
        'biology-chemistry': { subject1: 'biology', subject2: 'chemistry' },
        'biology-geography': { subject1: 'biology', subject2: 'geography' },
        'history-english': { subject1: 'world_history', subject2: 'english' },
        'history-law': { subject1: 'world_history', subject2: 'law' },
        'creative': { subject1: 'creative', subject2: 'creative' }
    };
    return combinations[combo] || { subject1: 'math', subject2: 'physics' };
}

function updateClassField() {
    if (!userProfile) userProfile = {};
    
    const classNumber = document.getElementById('profilePageClassNumber')?.value;
    const classLetter = document.getElementById('profilePageClassLetter')?.value;
    
    if (classNumber && classLetter) {
        userProfile.classNumber = classNumber;
        userProfile.classLetter = classLetter;
        userProfile.class = classNumber + classLetter;
        
        if (currentUser) saveProfileToSupabase(currentUser.id, userProfile);
        showToast(t('profileUpdated'), 'success');
    }
}

function updateENTProfileSubjects() {
    
}

// ==================== ENT SUBJECT MODAL ====================
function openSubjectModal(subject) {
    currentSubject = subject;
    const title = document.getElementById('subjectModalTitle');
    
    
    const allSubjectNames = getSubjectNames();
    
    
    let displayName;
    if (subject === 'profile1' && userProfile?.subject1) {
        displayName = allSubjectNames[userProfile.subject1] || t('profileSubject1');
        currentSubject = userProfile.subject1; 
    } else if (subject === 'profile2' && userProfile?.subject2) {
        displayName = allSubjectNames[userProfile.subject2] || t('profileSubject2');
        currentSubject = userProfile.subject2; 
    } else {
        const subjectNames = {
            history_kz: t('historyKZ'),
            reading: t('readingLit'),
            math_lit: t('mathLit'),
            profile1: t('profileSubject1'),
            profile2: t('profileSubject2')
        };
        displayName = subjectNames[subject] || allSubjectNames[subject] || subject;
    }
    
    if (title) {
        title.textContent = displayName;
    }
    
    openModal('subjectModal');
}

function subjectAction(action) {
    closeModal('subjectModal');
    
    
    if (action === 'topics') {
        showLibrary();
        filterBySubject(currentSubject);
    } else if (action === 'daily' || action === 'realtest') {
        
        showToast('Тест дайындалуда...', 'info');
    }
}

function startMockENT() {
    showToast('Пробный ЕНТ дайындалуда...', 'info');
}

// ==================== MATERIAL ACTION MODAL ====================
function openMaterialActionModal(material) {
    currentMaterialForAction = material;
    const title = document.getElementById('materialActionTitle');
    if (title) {
        title.textContent = material.title;
    }
    openModal('materialActionModal');
}

function materialAction(action) {
    closeModal('materialActionModal');
    
    if (!currentMaterialForAction) return;
    
    document.getElementById('materialInput').value = currentMaterialForAction.content;
    
    if (action === 'learn') {
        showInputSection();
    } else if (action === 'practice') {
        
        showInputSection();
        setTimeout(() => showModuleSelection(), 100);
    } else if (action === 'realtest') {
        showToast('Real Test режимі дайындалуда...', 'info');
    }
}

function openMaterialActionFromQuicklook() {
    closeModal('quicklookModal');
    if (quicklookMaterial) {
        openMaterialActionModal(quicklookMaterial);
    }
}

function handleStartBtn() {
    if (!currentUser) {
        showToast(t('pleaseLogin'), 'warning');
        openAuthModal('login');
        return;
    }
    showHome();
}

// ==================== SIDE PANELS ====================
function syncBlurOverlayState() {
    const hasActiveModal = !!document.querySelector('.modal-overlay.active');
    const hasActivePanel = !!document.getElementById('sidePanelLeft')?.classList.contains('active') ||
        !!document.getElementById('sidePanelRight')?.classList.contains('active');
    document.getElementById('blurOverlay')?.classList.toggle('active', hasActiveModal || hasActivePanel);
}

function openSidePanelLeft() {
    document.getElementById('sidePanelLeft')?.classList.add('active');
    syncBlurOverlayState();
}

function closeSidePanelLeft() {
    document.getElementById('sidePanelLeft')?.classList.remove('active');
    syncBlurOverlayState();
}

function openSidePanelRight() {
    
    if (isInTestEditorPage()) {
        showToast(t('saveOrExitEditor'), 'warning');
        return;
    }
    if (!currentUser) {
        showToast(t('pleaseLogin'), 'warning');
        openAuthModal('login');
        return;
    }
    document.getElementById('sidePanelRight')?.classList.add('active');
    syncBlurOverlayState();
}

function closeSidePanelRight() {
    document.getElementById('sidePanelRight')?.classList.remove('active');
    syncBlurOverlayState();
}

function closeAllSidePanels() {
    document.getElementById('sidePanelLeft')?.classList.remove('active');
    document.getElementById('sidePanelRight')?.classList.remove('active');
    syncBlurOverlayState();
}

// ==================== MODALS ====================
function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('hidden');
        modal.classList.add('active');
        closeAllSidePanels();
        syncBlurOverlayState();
    }
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('active');
        syncBlurOverlayState();
    }
}

function closeAllModals() {
    document.querySelectorAll('.modal-overlay.active').forEach(modal => {
        modal.classList.remove('active');
    });
    syncBlurOverlayState();
}

// ==================== THEME ====================
function setTheme(theme) {
    currentTheme = theme;
    document.body.setAttribute('data-theme', theme);
    
    
    saveUserPreferences();
    
    
    document.querySelectorAll('.style-card').forEach(card => {
        card.classList.toggle('selected', card.dataset.style === theme);
    });
    
    
    if (theme === 'flow') {
        initMatrixRain();
    } else {
        stopMatrixRain();
    }
}

function openStyleModal() {
    pendingTheme = currentTheme;
    document.querySelectorAll('.style-card').forEach(card => {
        card.classList.toggle('selected', card.dataset.style === currentTheme);
    });
    openModal('styleModal');
}

function selectStyle(style) {
    pendingTheme = style;
    document.querySelectorAll('.style-card').forEach(card => {
        card.classList.toggle('selected', card.dataset.style === style);
    });
}

function applySelectedStyle() {
    if (pendingTheme && pendingTheme !== currentTheme) {
        setTheme(pendingTheme);
        showToast(t('styleChanged'), 'success');
    }
    closeModal('styleModal');
}


function initMatrixRain() {
    const canvas = document.getElementById('matrixCanvas');
    if (!canvas) return;

    const ctx = canvas.getContext('2d', { alpha: true });
    if (!ctx) return;

    stopMatrixRain();

    const chars = 'OZGER0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ';
    const fontSize = SHOULD_USE_LIGHT_EFFECTS ? 16 : 14;
    const frameInterval = SHOULD_USE_LIGHT_EFFECTS ? (1000 / 20) : (1000 / 32);

    let drops = [];
    let columns = 0;
    let lastFrameAt = 0;

    function resizeCanvas() {
        const width = window.innerWidth;
        const height = window.innerHeight;
        const scale = SHOULD_USE_LIGHT_EFFECTS ? 1 : Math.min(window.devicePixelRatio || 1, 1.5);

        canvas.width = Math.floor(width * scale);
        canvas.height = Math.floor(height * scale);
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
        ctx.setTransform(scale, 0, 0, scale, 0, 0);

        columns = Math.max(12, Math.floor(width / fontSize));
        drops = Array.from({ length: columns }, () => Math.floor(Math.random() * 20));
    }

    resizeCanvas();

    matrixResizeHandler = debounce(() => {
        if (currentTheme === 'flow') resizeCanvas();
    }, 180);
    window.addEventListener('resize', matrixResizeHandler, { passive: true });

    matrixVisibilityHandler = () => {
        if (!document.hidden && currentTheme === 'flow' && !matrixAnimationId) {
            matrixAnimationId = requestAnimationFrame(draw);
        }
    };
    document.addEventListener('visibilitychange', matrixVisibilityHandler);

    function draw(now = 0) {
        if (currentTheme !== 'flow' || document.hidden) {
            matrixAnimationId = null;
            return;
        }

        if (now - lastFrameAt < frameInterval) {
            matrixAnimationId = requestAnimationFrame(draw);
            return;
        }
        lastFrameAt = now;

        const width = window.innerWidth;
        const height = window.innerHeight;
        ctx.fillStyle = SHOULD_USE_LIGHT_EFFECTS ? 'rgba(13, 17, 23, 0.16)' : 'rgba(13, 17, 23, 0.08)';
        ctx.fillRect(0, 0, width, height);

        ctx.font = `${fontSize}px monospace`;
        for (let index = 0; index < drops.length; index++) {
            const char = chars.charAt(Math.floor(Math.random() * chars.length));
            const x = index * fontSize;
            const y = drops[index] * fontSize;

            ctx.fillStyle = SHOULD_USE_LIGHT_EFFECTS
                ? 'rgba(0, 255, 65, 0.65)'
                : `rgba(0, 255, 65, ${Math.random() * 0.5 + 0.5})`;
            ctx.fillText(char, x, y);

            if (y > height && Math.random() > 0.975) {
                drops[index] = 0;
            }
            drops[index]++;
        }

        matrixAnimationId = requestAnimationFrame(draw);
    }

    matrixAnimationId = requestAnimationFrame(draw);
}

function stopMatrixRain() {
    if (matrixAnimationId) {
        cancelAnimationFrame(matrixAnimationId);
        matrixAnimationId = null;
    }
    if (matrixResizeHandler) {
        matrixResizeHandler.cancel?.();
        window.removeEventListener('resize', matrixResizeHandler);
        matrixResizeHandler = null;
    }
    if (matrixVisibilityHandler) {
        document.removeEventListener('visibilitychange', matrixVisibilityHandler);
        matrixVisibilityHandler = null;
    }
}

// ==================== LANGUAGE ====================
function setLanguage(lang) {
    currentLang = lang;
    
    
    saveUserPreferences();

    document.querySelectorAll('.lang-card').forEach(card => {
        card.classList.toggle('active', card.dataset.lang === lang);
    });

    applyTranslations();
    renderFaqContent();
}

function applyTranslations() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (i18n[currentLang] && i18n[currentLang][key]) {
            el.textContent = i18n[currentLang][key];
        }
    });
    
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        const key = el.getAttribute('data-i18n-placeholder');
        if (i18n[currentLang] && i18n[currentLang][key]) {
            el.placeholder = i18n[currentLang][key];
        }
    });
    
    
    const sidebarUsername = document.getElementById('sidebarUsername');
    if (sidebarUsername) {
        sidebarUsername.textContent = currentUser 
            ? (userProfile?.username || currentUser.user_metadata?.username || currentUser.email?.split('@')[0] || t('guest'))
            : t('guest');
    }

    
    updateDailyTestLimitUI();
    updateAIUsageUI();
}

// ==================== FAQ ====================
function showFaqSection(section) {
    document.querySelectorAll('.faq-tab').forEach(tab => {
        tab.classList.toggle('active', tab.textContent.includes(section === 'faq' ? t('faq') : t('siteGuide')));
    });
    
    document.getElementById('faqContent')?.classList.toggle('hidden', section !== 'faq');
    document.getElementById('guideContent')?.classList.toggle('hidden', section !== 'guide');
    
    if (section === 'faq') {
        renderFaqContent();
    } else {
        renderGuideContent();
    }
}


function getFaqItems() {
    const lang = currentLang || 'kk';
    const kkItems = [
        { q: "Бір функция істемей тұр", a: "Алдымен бетті жаңартыңыз. Интернетті тексеріңіз. Көмектеспесе, аккаунттан шығып қайта кіріңіз." },
        { q: "ЕНТ бөлімі қашан шығады?", a: "ЕНТ бөлімі ozger жобасының екінші айының басына жоспарланған." },
        { q: "Кітапхана тесттеріне лимит бар ма?", a: "Жоқ. Кітапхана тесттеріне лимит өшірілді, тесттерді шектеусіз өте аласыз." },
        { q: "Аватар немесе тест жүктелмесе не істеу керек?", a: "Файл форматын (jpg/png) және өлшемін тексеріңіз, қайта жүктеп көріңіз, аккаунтқа кіргеніңізге көз жеткізіңіз." },
        { q: "Неге батырмалар басылмай қалады?", a: "Бетті жаңартыңыз. Браузер аудармасы қосулы болса, өшіріңіз. Қажет болса аккаунтқа қайта кіріңіз." },
        { q: "Менде қосымша сұрақтар бар.", a:"Қосымша сұрақтарыңыз немесе мәселелеріңіз болса, біздің Telegram ботымызға @ozgercontantsbot арқылы хабарласыңыз." }
    ];
    const ruItems = [
        { q: "\u0423 \u043c\u0435\u043d\u044f \u043d\u0435 \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442 \u0444\u0443\u043d\u043a\u0446\u0438\u044f", a: "\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u043f\u0435\u0440\u0435\u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u0435 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0443. \u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0438\u043d\u0442\u0435\u0440\u043d\u0435\u0442. \u0415\u0441\u043b\u0438 \u043d\u0435 \u043f\u043e\u043c\u043e\u0433\u043b\u043e, \u0432\u044b\u0439\u0434\u0438\u0442\u0435 \u0438\u0437 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430 \u0438 \u0432\u043e\u0439\u0434\u0438\u0442\u0435 \u0441\u043d\u043e\u0432\u0430." },
        { q: "\u041a\u043e\u0433\u0434\u0430 \u0432\u044b\u0439\u0434\u0435\u0442 \u0440\u0430\u0437\u0434\u0435\u043b \u0415\u041d\u0422?", a: "\u0420\u0430\u0437\u0434\u0435\u043b \u0415\u041d\u0422 \u0437\u0430\u043f\u043b\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d \u043d\u0430 \u043d\u0430\u0447\u0430\u043b\u043e \u0432\u0442\u043e\u0440\u043e\u0433\u043e \u043c\u0435\u0441\u044f\u0446\u0430 \u043f\u0440\u043e\u0435\u043a\u0442\u0430 ozger." },
        { q: "\u0415\u0441\u0442\u044c \u043b\u0438 \u043b\u0438\u043c\u0438\u0442 \u043d\u0430 \u0442\u0435\u0441\u0442\u044b \u0431\u0438\u0431\u043b\u0438\u043e\u0442\u0435\u043a\u0438?", a: "\u041d\u0435\u0442. \u041b\u0438\u043c\u0438\u0442 \u043d\u0430 \u0442\u0435\u0441\u0442\u044b \u0431\u0438\u0431\u043b\u0438\u043e\u0442\u0435\u043a\u0438 \u043e\u0442\u043a\u043b\u044e\u0447\u0435\u043d, \u0432\u044b \u043c\u043e\u0436\u0435\u0442\u0435 \u043f\u0440\u043e\u0445\u043e\u0434\u0438\u0442\u044c \u0442\u0435\u0441\u0442\u044b \u0431\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u043e." },
        { q: "\u0427\u0442\u043e \u0434\u0435\u043b\u0430\u0442\u044c, \u0435\u0441\u043b\u0438 \u043d\u0435 \u0437\u0430\u0433\u0440\u0443\u0436\u0430\u0435\u0442\u0441\u044f \u0430\u0432\u0430\u0442\u0430\u0440 \u0438\u043b\u0438 \u0442\u0435\u0441\u0442?", a: "\u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0444\u043e\u0440\u043c\u0430\u0442 \u0444\u0430\u0439\u043b\u0430 (jpg/png), \u0443\u043c\u0435\u043d\u044c\u0448\u0438\u0442\u0435 \u0440\u0430\u0437\u043c\u0435\u0440, \u043f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0441\u043d\u043e\u0432\u0430 \u0438 \u0443\u0431\u0435\u0434\u0438\u0442\u0435\u0441\u044c, \u0447\u0442\u043e \u0432\u044b \u0432\u043e\u0448\u043b\u0438 \u0432 \u0430\u043a\u043a\u0430\u0443\u043d\u0442." },
        { q: "\u041f\u043e\u0447\u0435\u043c\u0443 \u043a\u043d\u043e\u043f\u043a\u0438 \u043d\u0435 \u043d\u0430\u0436\u0438\u043c\u0430\u044e\u0442\u0441\u044f?", a: "\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u0435 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0443. \u0415\u0441\u043b\u0438 \u0432\u043a\u043b\u044e\u0447\u0435\u043d \u043f\u0435\u0440\u0435\u0432\u043e\u0434 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430, \u043e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u0435 \u0435\u0433\u043e. \u041f\u0440\u0438 \u043d\u0435\u043e\u0431\u0445\u043e\u0434\u0438\u043c\u043e\u0441\u0442\u0438 \u043f\u0435\u0440\u0435\u0437\u0430\u0439\u0434\u0438\u0442\u0435 \u0432 \u0430\u043a\u043a\u0430\u0443\u043d\u0442." },
        { q: "\u0423 \u043c\u0435\u043d\u044f \u0435\u0441\u0442\u044c \u0434\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0435 \u0432\u043e\u043f\u0440\u043e\u0441\u044b.", a: "\u0415\u0441\u043b\u0438 \u0443 \u0432\u0430\u0441 \u0435\u0441\u0442\u044c \u0434\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0435 \u0432\u043e\u043f\u0440\u043e\u0441\u044b \u0438\u043b\u0438 \u043f\u0440\u043e\u0431\u043b\u0435\u043c\u044b, \u0441\u0432\u044f\u0436\u0438\u0442\u0435\u0441\u044c \u0441 \u043d\u0430\u0448\u0438\u043c \u0442\u0435\u043b\u0435\u0433\u0440\u0430\u043c-\u0431\u043e\u0442\u043e\u043c @ozgercontantsbot." }
    ];
    const enItems = [
        { q: "A feature is not working", a: "Reload the page first. Check your internet connection. If it still fails, sign out and sign back in." },
        { q: "When will the ENT section be released?", a: "The ENT section is planned for the beginning of the second month of the ozger project." },
        { q: "Is there a limit on library tests?", a: "No. The limit on library tests has been removed, you can take tests unlimited." },
        { q: "What if my avatar or test does not upload?", a: "Check the file format (jpg/png), reduce the size, retry, and make sure you are logged in." },
        { q: "Why are buttons not clickable?", a: "Refresh the page. If browser translation is enabled, turn it off. If needed, log in again." },
        { q: "I have additional questions.", a: "If you have additional questions or issues, contact our Telegram bot at @ozgercontantsbot." }
    ];
    const items = { kk: kkItems, ru: ruItems, en: enItems };
    return items[lang] || items.kk;
}

function getGuideItems() {
    const lang = currentLang || 'kk';
    const kkItems = [
        { q: "Сайт құрылымы", a: "Негізгі бөлімдер: AI Teacher, Library, Upload, Favorites, Profile, Classmates. Әр бөлім өз міндетіне жауап береді." },
        { q: "AI Teacher: Learn", a: "Learn режимі материалды кезең-кезеңмен түсіндіреді, жоспар көрсетеді және сұрақ қояды." },
        { q: "AI Teacher: Practice", a: "Practice режимі жаттығуға арналған: көп таңдаулы сұрақтар және прогресс бақылауы." },
        { q: "AI Teacher: Real Test", a: "Real Test — емтихан режимі. Подсказка мен түсіндірме жоқ, тек қорытынды нәтиже." },
        { q: "Library", a: "Library ішінде тесттерді пән, түр және көрінуі бойынша сүзуге болады. Өз тесттеріңіз де осында." },
        { q: "Upload және тест редакторы", a: "Upload бөлімінде тек тест жүктеледі. Редакторда сұрақ, жауап, дұрыс нұсқа және көрінуі орнатылады." },
        { q: "Favorites", a: "Ұнаған тесттерді Favorites-ке қосып, кейін тез қайта ашуға болады." },
        { q: "Profile және avatar", a: "Профильде қала, мектеп, сынып және комбинация сақталады. Аватарды ауыстыруға немесе өшіруге болады." },
        { q: "Classmates", a: "Classmates бөлімі сыныптастар мен олардың белсенділігін көрсетеді. Бірге дайындалуға ыңғайлы." },
        { q: "Лимиттер", a: "Кітапхана тесттеріне енді лимит жоқ. Қалағаныңызша өте аласыз." }
    ];
    const ruItems = [
        { q: "\u0421\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0430 \u0441\u0430\u0439\u0442\u0430", a: "\u041e\u0441\u043d\u043e\u0432\u043d\u044b\u0435 \u0440\u0430\u0437\u0434\u0435\u043b\u044b: AI Teacher, \u0411\u0438\u0431\u043b\u0438\u043e\u0442\u0435\u043a\u0430, Upload, Favorites, \u041f\u0440\u043e\u0444\u0438\u043b\u044c, Classmates. \u041a\u0430\u0436\u0434\u044b\u0439 \u0440\u0430\u0437\u0434\u0435\u043b \u043e\u0442\u0432\u0435\u0447\u0430\u0435\u0442 \u0437\u0430 \u0441\u0432\u043e\u044e \u0437\u0430\u0434\u0430\u0447\u0443." },
        { q: "AI Teacher: Learn", a: "Learn \u043e\u0431\u044a\u044f\u0441\u043d\u044f\u0435\u0442 \u043c\u0430\u0442\u0435\u0440\u0438\u0430\u043b \u043f\u043e \u0448\u0430\u0433\u0430\u043c, \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442 \u043f\u043b\u0430\u043d \u0438 \u0437\u0430\u0434\u0430\u0435\u0442 \u0432\u043e\u043f\u0440\u043e\u0441\u044b. \u042d\u0442\u043e \u0440\u0435\u0436\u0438\u043c \u043f\u043e\u043d\u0438\u043c\u0430\u043d\u0438\u044f." },
        { q: "AI Teacher: Practice", a: "Practice - \u0440\u0435\u0436\u0438\u043c \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0438. \u0415\u0441\u0442\u044c \u0432\u043e\u043f\u0440\u043e\u0441\u044b \u0438 \u0432\u0430\u0440\u0438\u0430\u043d\u0442\u044b \u043e\u0442\u0432\u0435\u0442\u043e\u0432, \u043f\u0440\u043e\u0433\u0440\u0435\u0441\u0441 \u0441\u043e\u0445\u0440\u0430\u043d\u044f\u0435\u0442\u0441\u044f." },
        { q: "AI Teacher: Real Test", a: "Real Test - \u044d\u043a\u0437\u0430\u043c\u0435\u043d\u0430\u0446\u0438\u043e\u043d\u043d\u044b\u0439 \u0440\u0435\u0436\u0438\u043c. \u041f\u043e\u0434\u0441\u043a\u0430\u0437\u043e\u043a \u0438 \u043e\u0431\u044a\u044f\u0441\u043d\u0435\u043d\u0438\u0439 \u043d\u0435\u0442, \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 - \u0431\u0430\u043b\u043b." },
        { q: "\u0411\u0438\u0431\u043b\u0438\u043e\u0442\u0435\u043a\u0430 (Library)", a: "\u0412 Library \u043c\u043e\u0436\u043d\u043e \u0444\u0438\u043b\u044c\u0442\u0440\u043e\u0432\u0430\u0442\u044c \u0442\u0435\u0441\u0442\u044b \u043f\u043e \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u0443, \u0442\u0438\u043f\u0443 \u0438 \u0432\u0438\u0434\u0438\u043c\u043e\u0441\u0442\u0438. \u0417\u0434\u0435\u0441\u044c \u0436\u0435 \u0432\u0430\u0448\u0438 \u0442\u0435\u0441\u0442\u044b." },
        { q: "Upload \u0438 \u0440\u0435\u0434\u0430\u043a\u0442\u043e\u0440 \u0442\u0435\u0441\u0442\u043e\u0432", a: "\u0412 Upload \u0437\u0430\u0433\u0440\u0443\u0436\u0430\u044e\u0442\u0441\u044f \u0442\u043e\u043b\u044c\u043a\u043e \u0442\u0435\u0441\u0442\u044b. \u0412 \u0440\u0435\u0434\u0430\u043a\u0442\u043e\u0440\u0435 \u0437\u0430\u0434\u0430\u044e\u0442\u0441\u044f \u0432\u043e\u043f\u0440\u043e\u0441\u044b, \u043e\u0442\u0432\u0435\u0442\u044b, \u043f\u0440\u0430\u0432\u0438\u043b\u044c\u043d\u044b\u0439 \u0432\u0430\u0440\u0438\u0430\u043d\u0442 \u0438 \u0432\u0438\u0434\u0438\u043c\u043e\u0441\u0442\u044c." },
        { q: "Favorites", a: "\u0414\u043e\u0431\u0430\u0432\u043b\u044f\u0439\u0442\u0435 \u043f\u043e\u043d\u0440\u0430\u0432\u0438\u0432\u0448\u0438\u0435\u0441\u044f \u0442\u0435\u0441\u0442\u044b \u0432 Favorites, \u0447\u0442\u043e\u0431\u044b \u0431\u044b\u0441\u0442\u0440\u043e \u0432\u0435\u0440\u043d\u0443\u0442\u044c\u0441\u044f \u043a \u043d\u0438\u043c." },
        { q: "\u041f\u0440\u043e\u0444\u0438\u043b\u044c \u0438 \u0430\u0432\u0430\u0442\u0430\u0440", a: "\u0412 \u043f\u0440\u043e\u0444\u0438\u043b\u0435 \u0445\u0440\u0430\u043d\u044f\u0442\u0441\u044f \u0433\u043e\u0440\u043e\u0434, \u0448\u043a\u043e\u043b\u0430, \u043a\u043b\u0430\u0441\u0441 \u0438 \u043a\u043e\u043c\u0431\u0438\u043d\u0430\u0446\u0438\u044f. \u0410\u0432\u0430\u0442\u0430\u0440 \u043c\u043e\u0436\u043d\u043e \u0441\u043c\u0435\u043d\u0438\u0442\u044c \u0438\u043b\u0438 \u0443\u0434\u0430\u043b\u0438\u0442\u044c." },
        { q: "Classmates", a: "Classmates \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442 \u043e\u0434\u043d\u043e\u043a\u043b\u0430\u0441\u0441\u043d\u0438\u043a\u043e\u0432 \u0438 \u0438\u0445 \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0441\u0442\u044c. \u0423\u0434\u043e\u0431\u043d\u043e \u0443\u0447\u0438\u0442\u044c\u0441\u044f \u0432\u043c\u0435\u0441\u0442\u0435." },
        { q: "\u041b\u0438\u043c\u0438\u0442\u044b", a: "\u041d\u0430 \u0442\u0435\u0441\u0442\u044b \u0438\u0437 \u0431\u0438\u0431\u043b\u0438\u043e\u0442\u0435\u043a\u0438 \u043b\u0438\u043c\u0438\u0442 \u043e\u0442\u043a\u043b\u044e\u0447\u0435\u043d. \u041c\u043e\u0436\u043d\u043e \u043f\u0440\u043e\u0445\u043e\u0434\u0438\u0442\u044c \u0431\u0435\u0437 \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u0439." }
    ];
    const enItems = [
        { q: "Site structure", a: "Main sections: AI Teacher, Library, Upload, Favorites, Profile, Classmates. Each section has its own purpose." },
        { q: "AI Teacher: Learn", a: "Learn explains the topic step by step, shows a plan, and asks questions. It is for understanding." },
        { q: "AI Teacher: Practice", a: "Practice is for training. You answer questions with multiple choices and track progress." },
        { q: "AI Teacher: Real Test", a: "Real Test is exam mode. No hints or explanations, only final score." },
        { q: "Library", a: "In Library you can filter tests by subject, type, and visibility. Your own tests are here too." },
        { q: "Upload and editor", a: "Upload is for tests only. In the editor you set questions, answers, the correct option, and visibility." },
        { q: "Favorites", a: "Save tests to Favorites to return quickly later." },
        { q: "Profile and avatar", a: "Profile stores city, school, class, and combination. You can change or delete your avatar." },
        { q: "Classmates", a: "Classmates shows classmates and their activity. Useful for studying together." },
        { q: "Limits", a: "Library tests are unlimited now." }
    ];
    const items = { kk: kkItems, ru: ruItems, en: enItems };
    return items[lang] || items.kk;
}

function renderCollapsibleList(container, items) {
    container.innerHTML = '';

    items.forEach(item => {
        const wrapper = document.createElement('div');
        wrapper.className = 'faq-item';

        const question = document.createElement('button');
        question.type = 'button';
        question.className = 'faq-question';
        question.textContent = item.q;

        const toggle = document.createElement('span');
        toggle.className = 'faq-toggle';
        question.appendChild(toggle);

        const answer = document.createElement('div');
        answer.className = 'faq-answer';
        answer.textContent = item.a;

        question.addEventListener('click', () => {
            wrapper.classList.toggle('open');
            question.setAttribute('aria-expanded', wrapper.classList.contains('open'));
        });

        wrapper.appendChild(question);
        wrapper.appendChild(answer);
        container.appendChild(wrapper);
    });
}

function renderFaqContent() {
    const faqContent = document.getElementById('faqContent');
    if (!faqContent) return;
    renderCollapsibleList(faqContent, getFaqItems());
}

function renderGuideContent() {
    const guideContent = document.getElementById('guideContent');
    if (!guideContent) return;
    renderCollapsibleList(guideContent, getGuideItems());
}

// ==================== AUTH ====================
function openAuthModal(mode = 'login') {
    regStep = 1;
    regData = {};
    renderAuthForm(mode);
    updateAuthSteps();
    openModal('authModal');
}

function updateAuthSteps() {
    document.querySelectorAll('.step').forEach(step => {
        const stepNum = parseInt(step.dataset.step);
        step.classList.toggle('active', stepNum === regStep);
        step.classList.toggle('completed', stepNum < regStep);
    });
    
    
    const stepsContainer = document.getElementById('authSteps');
    if (stepsContainer) {
        stepsContainer.style.display = regStep === 0 ? 'none' : 'flex';
    }
}

function bindAuthNoneSelectStyling() {
    const selectIds = ['regSchool', 'regClassNumber', 'regClassLetter', 'regSubjectCombination'];

    selectIds.forEach((id) => {
        const select = document.getElementById(id);
        if (!select) return;

        const syncState = () => {
            select.classList.toggle('is-none-selected', select.value === 'none');
        };

        syncState();
        select.addEventListener('change', syncState);
    });
}

function renderAuthForm(mode = 'login') {
    const isForgot = mode === 'forgot';
    const isLogin = mode === 'login' && !isForgot;
    const isReset = mode === 'reset';
    const container = document.getElementById('authFormContainer');
    const title = document.getElementById('authModalTitle');
    
    if (title) {
        if (isForgot) {
            title.textContent = t('forgotPassword');
        } else {
            title.textContent = isLogin ? t('login') : t('register');
        }
    }
    
    if (isReset) {
        console.log('Rendering reset password form');
        console.log('Container found:', !!container);
        console.log('Title element found:', !!title);

        regStep = 0;
        updateAuthSteps();

        if (container) {
            title.textContent = 'Сброс пароля';

            const formHtml = `
                <form class="auth-form" id="authForm">
                    <div class="form-group">
                        <label class="form-label">Новый пароль</label>
                        <input type="password" class="form-input" id="newPassword" placeholder="Введите новый пароль" required>
                    </div>
                    <div class="form-group">
                        <label class="form-label">Подтвердите пароль</label>
                        <input type="password" class="form-input" id="confirmNewPassword" placeholder="Подтвердите новый пароль" required>
                    </div>
                    <button type="submit" class="btn btn-primary" style="width:100%;padding:14px;">
                        Обновить пароль
                    </button>
                </form>
            `;
            console.log('Setting container innerHTML for reset form');
            container.innerHTML = formHtml;

            document.getElementById('authForm')?.addEventListener('submit', (e) => {
                e.preventDefault();
                handleResetPassword();
            });
        }
        return;
    }

    
    if (isForgot) {
        regStep = 0;
        updateAuthSteps();
        
        if (container) {
            container.innerHTML = `
                <form class="auth-form" id="authForm">
                    <div class="form-group">
                        <label class="form-label">${t('emailPlaceholder')}</label>
                        <input type="email" class="form-input" id="resetEmail" placeholder="${t('emailPlaceholder')}" required>
                    </div>
                    <p class="auth-hint">
                        ${t('resetPassword')}
                    </p>
                    <button type="submit" class="btn btn-primary" style="width: 100%; padding: 14px;">
                        ${t('sendResetLink')}
                    </button>
                </form>
                <div class="auth-switch">
                    ${t('haveAccount')}
                    <span class="auth-switch-link" id="backToLog">${t('signUp')}</span>
                </div>
            `;
            
            document.getElementById('authForm')?.addEventListener('submit', (e) => {
                e.preventDefault();
                handleForgotPassword();
            });
            
            document.getElementById('backToLog')?.addEventListener('click', () => {
                renderAuthForm('login');
            });
        }
        return;
    }
    
    
    if (isLogin) {
        regStep = 0;
        updateAuthSteps();
        
        if (container) {
            container.innerHTML = `
                <form class="auth-form" id="authForm">
                    <div class="form-group">
                        <input type="text" class="form-input" id="authEmail" placeholder="${t('usernameOrEmail')}" required>
                    </div>
                    <div class="form-group">
                        <input type="password" class="form-input" id="authPassword" placeholder="${t('passwordPlaceholder')}" required>
                        <div class="password-toggle-wrapper">
                            <input type="checkbox" id="showPasswordLogin" class="password-toggle-checkbox">
                            <label for="showPasswordLogin" class="password-toggle-label">
                                <span class="password-toggle-icon">
                                    <svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
                                </span>
                                <span>${t('showPassword')}</span>
                            </label>
                        </div>
                    </div>
                     <span class="auth-switch-link" id="forgotPassword">${t('forgotPassword')}</span>
                    <button type="submit" class="btn btn-primary" style="width: 100%; padding: 14px;">
                        ${t('signIn')}
                    </button>
                </form>
                <div class="auth-switch">
                    ${t('noAccount')}
                    <span class="auth-switch-link" id="authSwitchLink">${t('signUp')}</span>
                </div>
            `;
            
            document.getElementById('authForm')?.addEventListener('submit', (e) => {
                e.preventDefault();
                handleAuth(true);
            });
            
            document.getElementById('showPasswordLogin')?.addEventListener('change', (e) => {
                const passwordField = document.getElementById('authPassword');
                if (passwordField) {
                    passwordField.type = e.target.checked ? 'text' : 'password';
                }
            });
            
            document.getElementById('forgotPassword')?.addEventListener('click', () => {
                renderAuthForm('forgot');
            });
            
            document.getElementById('authSwitchLink')?.addEventListener('click', () => {
                regStep = 1;
                renderAuthForm('register');
            });
        }
        return;
    }
    
    
    if (container) {
        if (regStep === 1) {
            container.innerHTML = `
                <form class="auth-form form-step" id="authForm">
                    <div class="form-group">
                        <label class="form-label">${t('usernamePlaceholder')}</label>
                        <input type="text" class="form-input" id="regUsername" value="${regData.username || ''}" placeholder="${t('usernamePlaceholder')}" required>
                    </div>
                    <div class="form-group">
                        <label class="form-label">${t('country')}</label>
                        <select class="form-input form-select" id="regCountry">
                            <option value="kz" ${regData.country === 'kz' ? 'selected' : ''}>Қазақстан</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label class="form-label">${t('city')}</label>
                        <select class="form-input form-select" id="regCity">
                            <option value="almaty" ${regData.city === 'almaty' ? 'selected' : ''}>Алматы</option>
                            <option value="astana" ${regData.city === 'astana' ? 'selected' : ''}>Астана</option>
                            <option value="karaganda" ${regData.city === 'karaganda' ? 'selected' : ''}>Қарағанды</option>
                            <option value="other" ${regData.city === 'other' ? 'selected' : ''}>Басқа</option>
                        </select>
                    </div>
                    <div class="step-nav">
                        <div></div>
                        <button type="submit" class="btn btn-primary">${t('nextStep')} →</button>
                    </div>
                </form>
                <div class="auth-switch">
                    ${t('haveAccount')}
                    <span class="auth-switch-link" id="authSwitchLink">${t('signIn')}</span>
                </div>
            `;
        } else if (regStep === 2) {
            container.innerHTML = `
                <form class="auth-form form-step" id="authForm">
                    <div class="form-group">
                        <label class="form-label">${t('school')}</label>
                        <select class="form-input form-select" id="regSchool">
                            <option value="none" disabled ${!regData.school || regData.school === 'none' ? 'selected' : ''}>none</option>
                            <option value="dostyq" ${regData.school === 'dostyq' ? 'selected' : ''}>Dostyq School</option>
                            <option value="other" ${regData.school === 'other' ? 'selected' : ''}>Басқа</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label class="form-label">${t('classNumber')}</label>
                            <select class="form-input form-select" id="regClassNumber">
                                <option value="none" disabled ${!regData.classNumber || regData.classNumber === 'none' ? 'selected' : ''}>none</option>
                                <option value="10" ${regData.classNumber === '10' ? 'selected' : ''}>10</option>
                                <option value="11" ${regData.classNumber === '11' ? 'selected' : ''}>11</option>
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label">${t('classLetter')}</label>
                            <select class="form-input form-select" id="regClassLetter">
                                <option value="none" disabled ${!regData.classLetter || regData.classLetter === 'none' ? 'selected' : ''}>none</option>
                                <option value="А" ${regData.classLetter === 'А' ? 'selected' : ''}>А</option>
                                <option value="Ә" ${regData.classLetter === 'Ә' ? 'selected' : ''}>Ә</option>
                                <option value="Б" ${regData.classLetter === 'Б' ? 'selected' : ''}>Б</option>
                                <option value="В" ${regData.classLetter === 'В' ? 'selected' : ''}>В</option>
                                <option value="Г" ${regData.classLetter === 'Г' ? 'selected' : ''}>Г</option>
                                <option value="Ғ" ${regData.classLetter === 'Ғ' ? 'selected' : ''}>Ғ</option>
                                <option value="Д" ${regData.classLetter === 'Д' ? 'selected' : ''}>Д</option>
                                <option value="Е" ${regData.classLetter === 'Е' ? 'selected' : ''}>Е</option>
                                <option value="Ж" ${regData.classLetter === 'Ж' ? 'selected' : ''}>Ж</option>
                                <option value="З" ${regData.classLetter === 'З' ? 'selected' : ''}>З</option>
                                <option value="И" ${regData.classLetter === 'И' ? 'selected' : ''}>И</option>
                            </select>
                        </div>
                    </div>
                    <div class="form-group">
                        <label class="form-label">${t('profileCombination')}</label>
                        <select class="form-input form-select" id="regSubjectCombination">
                            <option value="none" disabled ${!regData.subjectCombination || regData.subjectCombination === 'none' ? 'selected' : ''}>none</option>
                            <option value="informatics-math" ${regData.subjectCombination === 'informatics-math' ? 'selected' : ''}>${t('comboInformaticsMath')}</option>
                            <option value="geography-math" ${regData.subjectCombination === 'geography-math' ? 'selected' : ''}>${t('comboGeographyMath')}</option>
                            <option value="physics-math" ${regData.subjectCombination === 'physics-math' ? 'selected' : ''}>${t('comboPhysicsMath')}</option>
                            <option value="biology-chemistry" ${regData.subjectCombination === 'biology-chemistry' ? 'selected' : ''}>${t('comboBiologyChemistry')}</option>
                            <option value="biology-geography" ${regData.subjectCombination === 'biology-geography' ? 'selected' : ''}>${t('comboBiologyGeography')}</option>
                            <option value="history-english" ${regData.subjectCombination === 'history-english' ? 'selected' : ''}>${t('comboHistoryEnglish')}</option>
                            <option value="history-law" ${regData.subjectCombination === 'history-law' ? 'selected' : ''}>${t('comboHistoryLaw')}</option>
                            <option value="creative" ${regData.subjectCombination === 'creative' ? 'selected' : ''}>${t('comboCreative')}</option>
                        </select>
                    </div>
                    <div class="step-nav">
                        <button type="button" class="btn btn-ghost" onclick="prevRegStep()">← ${t('prevStep')}</button>
                        <button type="submit" class="btn btn-primary">${t('nextStep')} →</button>
                    </div>
                </form>
            `;
        } else if (regStep === 3) {
            container.innerHTML = `
                <form class="auth-form form-step" id="authForm">
                    <div class="form-group">
                        <label class="form-label">Gmail</label>
                        <input type="email" class="form-input" id="regEmail" value="${regData.email || ''}" placeholder="${t('emailPlaceholder')}" required>
                    </div>
                    <div class="form-group">
                        <label class="form-label">${t('passwordPlaceholder')}</label>
                        <input type="password" class="form-input" id="regPassword" placeholder="${t('passwordPlaceholder')}" required>
                    </div>
                    <div class="form-group">
                        <label class="form-label">${t('confirmPassword')}</label>
                        <input type="password" class="form-input" id="regPasswordConfirm" placeholder="${t('confirmPassword')}" required>
                        <div class="password-toggle-wrapper">
                            <input type="checkbox" id="showPasswordReg" class="password-toggle-checkbox">
                            <label for="showPasswordReg" class="password-toggle-label">
                                <span class="password-toggle-icon">
                                    <svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
                                </span>
                                <span>${t('showPassword')}</span>
                            </label>
                        </div>
                    </div>
                    <div class="step-nav">
                        <button type="button" class="btn btn-ghost" onclick="prevRegStep()">← ${t('prevStep')}</button>
                        <button type="submit" class="btn btn-primary">${t('signUp')}</button>
                    </div>
                </form>
            `;
            
            
            setTimeout(() => {
                document.getElementById('showPasswordReg')?.addEventListener('change', (e) => {
                    const passwordField = document.getElementById('regPassword');
                    const confirmField = document.getElementById('regPasswordConfirm');
                    const type = e.target.checked ? 'text' : 'password';
                    if (passwordField) passwordField.type = type;
                    if (confirmField) confirmField.type = type;
                });
            }, 0);
        }
        
        document.getElementById('authForm')?.addEventListener('submit', (e) => {
            e.preventDefault();
            handleRegStep();
        });
        
        document.getElementById('authSwitchLink')?.addEventListener('click', () => {
            renderAuthForm('login');
        });
        
        updateAuthSteps();
        if (regStep === 2) {
            bindAuthNoneSelectStyling();
        }
    }
}

function prevRegStep() {
    if (regStep > 1) {
        regStep--;
        renderAuthForm('register');
    }
}

async function handleRegStep() {
    if (regStep === 1) {
        const rawUsername = document.getElementById('regUsername')?.value.trim();
        regData.username = rawUsername ? rawUsername.toLowerCase() : '';
        regData.country = document.getElementById('regCountry')?.value;
        regData.city = document.getElementById('regCity')?.value;
        
        if (!regData.username) {
            showToast(t('fillAllFields'), 'warning');
            return;
        }
        
        
        const usernameRegex = /^[a-z][a-z0-9_]{2,19}$/;
        if (!usernameRegex.test(regData.username)) {
            showToast(t('usernameEnglishOnly'), 'warning');
            return;
        }
        
        
        const submitBtn = document.querySelector('#authForm button[type="submit"]');
        const originalBtnText = submitBtn?.textContent;
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = t('checkingUsername');
        }
        
        try {
            
            const usernameExists = await checkUsernameExists(regData.username);
            if (usernameExists) {
                showToast(t('usernameTaken'), 'error');
                return;
            }
            
            regStep = 2;
            renderAuthForm('register');
        } finally {
            
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.textContent = originalBtnText;
            }
        }
    } else if (regStep === 2) {
        regData.school = document.getElementById('regSchool')?.value;
        regData.classNumber = document.getElementById('regClassNumber')?.value;
        regData.classLetter = document.getElementById('regClassLetter')?.value;
        regData.subjectCombination = document.getElementById('regSubjectCombination')?.value;
        if (!regData.school || regData.school === 'none' ||
            !regData.classNumber || regData.classNumber === 'none' ||
            !regData.classLetter || regData.classLetter === 'none' ||
            !regData.subjectCombination || regData.subjectCombination === 'none') {
            showToast(t('fillAllFields'), 'warning');
            return;
        }
        regData.class = regData.classNumber + regData.classLetter; 
        const parsed = parseSubjectCombination(regData.subjectCombination);
        regData.subject1 = parsed.subject1;
        regData.subject2 = parsed.subject2;
        
        regStep = 3;
        renderAuthForm('register');
    } else if (regStep === 3) {
        regData.email = document.getElementById('regEmail')?.value.trim();
        const password = document.getElementById('regPassword')?.value;
        const confirmPassword = document.getElementById('regPasswordConfirm')?.value;
        
        if (!regData.email || !password) {
            showToast(t('fillAllFields'), 'warning');
            return;
        }
        
        if (password !== confirmPassword) {
            showToast(t('passwordMismatch'), 'warning');
            return;
        }
        
        
        completeRegistration(password);
    }
}

// ==================== USERNAME VALIDATION ====================
async function checkUsernameExists(username) {
    if (!supabaseClient || !username) return false;
    
    try {
        const supabaseUrl = supabaseClient.supabaseUrl;
        const supabaseKey = supabaseClient.supabaseKey;
        
        
        const response = await fetch(
            `${supabaseUrl}/rest/v1/profiles?username=eq.${encodeURIComponent(username)}&select=username`,
            {
                headers: {
                    'apikey': supabaseKey,
                    'Authorization': `Bearer ${supabaseKey}`
                }
            }
        );
        
        if (response.ok) {
            const profiles = await response.json();
            if (profiles && profiles.length > 0) {
                console.log('Username already exists in profiles:', username);
                return true;
            }
        }
        
        
        
        
        return false;
    } catch (error) {
        console.error('Error checking username:', error);
        
        return false;
    }
}

// ==================== SUPABASE PROFILE SYNC ====================
async function saveProfileToSupabase(userId, profile) {
    if (!supabaseClient || !userId || !profile) {
        console.error('saveProfileToSupabase: missing required params', { supabaseClient: !!supabaseClient, userId, profile: !!profile });
        return false;
    }

    try {
        
        const profileData = {
            user_id: userId,
            updated_at: new Date().toISOString()
        };

        const setIfDefined = (key, value) => {
            if (value === undefined || value === null) return;
            if (typeof value === 'string' && value.trim() === '') return;
            profileData[key] = value;
        };

        setIfDefined('username', profile.username);
        setIfDefined('email', profile.email || currentUser?.email);
        setIfDefined('country', profile.country || 'kz');
        setIfDefined('city', profile.city);
        setIfDefined('school', profile.school);
        setIfDefined('class', profile.class);
        setIfDefined('class_number', profile.classNumber || profile.class?.substring(0, 2));
        setIfDefined('class_letter', profile.classLetter || profile.class?.substring(2));
        setIfDefined('subject_combination', profile.subjectCombination);
        setIfDefined('subject1', profile.subject1);
        setIfDefined('subject2', profile.subject2);
        setIfDefined('avatar_url', userAvatar || null);

        console.log('Saving profile to Supabase:', profileData);

        
        const { data, error } = await supabaseClient
            .from('profiles')
            .upsert(profileData, { 
                onConflict: 'user_id',
                ignoreDuplicates: false 
            })
            .select();

        if (error) {
            console.error('Error saving profile to Supabase:', error);
            return false;
        }

        console.log('Profile saved to Supabase:', data);
        
        
        localStorage.removeItem('ozger_pending_profile');
        
        return true;
    } catch (error) {
        console.error('Error saving profile:', error);
        return false;
    }
}

function buildProfileFromMetadata(meta) {
    if (!meta) return {};
    const profile = {};
    const setIf = (key, value) => {
        if (value === undefined || value === null) return;
        if (typeof value === 'string' && value.trim() === '') return;
        profile[key] = value;
    };

    const classValue = meta.class || ((meta.class_number || meta.classNumber) && (meta.class_letter || meta.classLetter)
        ? `${meta.class_number || meta.classNumber}${meta.class_letter || meta.classLetter}`
        : '');

    setIf('username', meta.username);
    setIf('country', meta.country);
    setIf('city', meta.city);
    setIf('school', meta.school);
    setIf('class', classValue);
    setIf('classNumber', meta.class_number || meta.classNumber || (classValue ? classValue.substring(0, 2) : undefined));
    setIf('classLetter', meta.class_letter || meta.classLetter || (classValue ? classValue.substring(2) : undefined));
    setIf('subjectCombination', meta.subject_combination || meta.subjectCombination);
    setIf('subject1', meta.subject1);
    setIf('subject2', meta.subject2);

    return profile;
}


async function loadProfileFromSupabase(userId) {
    if (!supabaseClient || !userId) return null;
    
    try {
        const { data, error } = await supabaseClient
            .from('profiles')
            .select('*')
            .eq('user_id', userId)
            .single();
        
        if (error && error.code !== 'PGRST116') {
            console.error('Error loading profile from Supabase:', error);
            return null;
        }
        
        if (data) {
            
            userProfile = {
                username: data.username,
                country: data.country,
                city: data.city,
                school: data.school,
                class: data.class || (data.class_number + data.class_letter),
                classNumber: data.class_number || data.class?.substring(0, 2),
                classLetter: data.class_letter || data.class?.substring(2),
                subjectCombination: data.subject_combination,
                subject1: data.subject1,
                subject2: data.subject2,
                email: currentUser?.email
            };
            
            
            if (data.avatar_url) {
                userAvatar = data.avatar_url;
                updateAvatarUI(userAvatar);
            } else {
                userAvatar = null;
            }

            
            const metaProfile = buildProfileFromMetadata(currentUser?.user_metadata);
            const syncFields = [
                'username',
                'country',
                'city',
                'school',
                'class',
                'classNumber',
                'classLetter',
                'subjectCombination',
                'subject1',
                'subject2'
            ];
            let needsSync = false;
            syncFields.forEach((field) => {
                if (!userProfile[field] && metaProfile[field]) {
                    userProfile[field] = metaProfile[field];
                    needsSync = true;
                }
            });
            if (needsSync) {
                await saveProfileToSupabase(userId, userProfile);
            }
            
            console.log('Profile loaded from Supabase:', userProfile);
            return userProfile;
        }

        
        const metaProfile = buildProfileFromMetadata(currentUser?.user_metadata);
        if (Object.keys(metaProfile).length > 0) {
            userProfile = { ...metaProfile, email: currentUser?.email };
            await saveProfileToSupabase(userId, userProfile);
            return userProfile;
        }

        userProfile = null;
        userAvatar = null;
        updateAvatarUI(null);
        return null;
    } catch (error) {
        console.error('Error loading profile:', error);
        return null;
    }
}

// ==================== USER PREFERENCES (Language, Theme) - localStorage ====================
function loadUserPreferences() {
    try {
        
        const savedPrefs = localStorage.getItem('ozger_preferences');
        if (savedPrefs) {
            const prefs = JSON.parse(savedPrefs);
            currentLang = prefs.language || 'kk';
            currentTheme = prefs.theme || 'basic';
            console.log('Preferences loaded from localStorage:', { lang: currentLang, theme: currentTheme });
        }
    } catch (error) {
        console.error('Error loading preferences from localStorage:', error);
    }

    
    document.body.setAttribute('data-theme', currentTheme);
    document.querySelectorAll('.style-card').forEach(card => {
        card.classList.toggle('selected', card.dataset.style === currentTheme);
    });
    if (currentTheme === 'flow') {
        initMatrixRain();
    } else {
        stopMatrixRain();
    }
    applyTranslations();
}

function saveUserPreferences() {
    try {
        
        const prefs = {
            language: currentLang,
            theme: currentTheme,
            updatedAt: new Date().toISOString()
        };
        localStorage.setItem('ozger_preferences', JSON.stringify(prefs));
        console.log('Preferences saved to localStorage');
    } catch (error) {
        console.error('Error saving preferences to localStorage:', error);
    }
}

// ==================== USER FAVORITES ====================
async function loadUserFavorites() {
    if (!currentUser) {
        favorites = [];
        return;
    }
    if (!supabaseClient) {
        const ready = await ensureSupabaseReady();
        if (!ready) {
            favorites = [];
            return;
        }
    }
    
    try {
        const { data, error } = await supabaseClient
            .from('user_favorites')
            .select('test_id')
            .eq('user_id', currentUser.id);

        if (error) {
            console.error('Error loading favorites:', error);
            favorites = [];
            return;
        }

        favorites = (data || []).map(f => f.test_id);
        console.log('Favorites loaded:', favorites.length);
    } catch (error) {
        console.error('Error loading favorites:', error);
        favorites = [];
    }
}

async function addToFavoritesSupabase(testId) {
    if (!currentUser) return false;
    if (!supabaseClient) {
        const ready = await ensureSupabaseReady();
        if (!ready) return false;
    }
    
    try {
        const { error } = await supabaseClient
            .from('user_favorites')
            .insert([{ user_id: currentUser.id, test_id: testId }]);

        if (error) {
            console.error('Error adding to favorites:', error);
            return false;
        }
        return true;
    } catch (error) {
        console.error('Error adding to favorites:', error);
        return false;
    }
}

async function removeFromFavoritesSupabase(testId) {
    if (!currentUser) return false;
    if (!supabaseClient) {
        const ready = await ensureSupabaseReady();
        if (!ready) return false;
    }
    
    try {
        const { error } = await supabaseClient
            .from('user_favorites')
            .delete()
            .eq('user_id', currentUser.id)
            .eq('test_id', testId);

        if (error) {
            console.error('Error removing from favorites:', error);
            return false;
        }
        return true;
    } catch (error) {
        console.error('Error removing from favorites:', error);
        return false;
    }
}


// ==================== USER STATS ====================
async function loadUserStats() {
    if (!currentUser) return;
    if (!supabaseClient) {
        const ready = await ensureSupabaseReady();
        if (!ready) return;
    }
    
    try {
        const supabaseUrl = supabaseClient.supabaseUrl;
        const supabaseKey = supabaseClient.supabaseKey;
        const { data: sessionData } = await supabaseClient.auth.getSession();
        const token = sessionData?.session?.access_token;
        
        const data = await fetchJsonWithControl(
            `${supabaseUrl}/rest/v1/user_stats?user_id=eq.${currentUser.id}&select=*`,
            {
                headers: {
                    'apikey': supabaseKey,
                    'Authorization': `Bearer ${token || supabaseKey}`
                },
                timeoutMs: 4500,
                cacheKey: `user-stats:${currentUser.id}`,
                cacheTtlMs: 15 * 1000
            }
        );

        if (Array.isArray(data) && data.length > 0) {
            const stats = data[0];
            userStats = {
                totalTests: stats.total_tests || 0,
                guessStreak: stats.guess_streak || 0,
                guessBestStreak: stats.guess_best_streak || 0,
                entBestScore: stats.ent_best_score || 0,
                entTestsCompleted: stats.ent_tests_completed || 0
            };

            guessStreak = userStats.guessStreak || 0;
            guessBestStreak = userStats.guessBestStreak || 0;
        }
    } catch (error) {
        console.error('Error loading stats:', error);
    }
}

async function saveUserStats() {
    if (!supabaseClient || !currentUser) return;
    
    try {
        const { error } = await supabaseClient
            .from('user_stats')
            .upsert({
                user_id: currentUser.id,
                total_tests: userStats.totalTests,
                guess_streak: userStats.guessStreak,
                guess_best_streak: userStats.guessBestStreak,
                ent_best_score: userStats.entBestScore,
                ent_tests_completed: userStats.entTestsCompleted,
                updated_at: new Date().toISOString()
            }, { onConflict: 'user_id' });

        if (error) {
            console.error('Error saving stats:', error);
        }
    } catch (error) {
        console.error('Error saving stats:', error);
    }
}


async function loadAllUserData() {
    if (!currentUser) return;

    await Promise.all([
        loadUserFavorites(),
        loadUserStats()
    ]);
}

async function saveAvatarToSupabase(avatarUrl) {
    if (!currentUser) return false;
    if (!supabaseClient) {
        const ready = await ensureSupabaseReady();
        if (!ready) return false;
    }
    
    try {
        const username = userProfile?.username || currentUser.user_metadata?.username || currentUser.email?.split('@')[0];
        const payload = {
            user_id: currentUser.id,
            avatar_url: avatarUrl,
            updated_at: new Date().toISOString()
        };
        if (username) payload.username = username;
        if (currentUser.email) payload.email = currentUser.email;

        const { error } = await supabaseClient
            .from('profiles')
            .upsert(payload, { onConflict: 'user_id' });
        
        if (error) {
            console.error('Error saving avatar:', error);
            return false;
        }
        return true;
    } catch (error) {
        console.error('Error saving avatar:', error);
        return false;
    }
}

async function completeRegistration(password) {
    if (!supabaseClient) {
        
        userProfile = {
            username: regData.username,
            country: regData.country,
            city: regData.city,
            school: regData.school,
            class: regData.class,
            subjectCombination: regData.subjectCombination,
            subject1: regData.subject1,
            subject2: regData.subject2,
            email: regData.email
        };
        
        currentUser = { email: regData.email, user_metadata: { username: regData.username } };
        showToast(t('registerSuccess'), 'success');
        closeModal('authModal');
        updateAuthUI();
        return;
    }

    try {
        const normalizedUsername = regData.username ? regData.username.toLowerCase() : '';
        regData.username = normalizedUsername;
        const { data, error } = await supabaseClient.auth.signUp({
            email: regData.email,
            password,
            options: {
                data: {
                    username: normalizedUsername,
                    country: regData.country,
                    city: regData.city,
                    school: regData.school,
                    class: regData.class,
                    subjectCombination: regData.subjectCombination,
                    subject1: regData.subject1,
                    subject2: regData.subject2
                },
                emailRedirectTo: `${window.location.origin}?type=signup`
            }
        });

        if (error) throw error;

        
        if (data.user && data.session) {
            currentUser = data.user;
            
            userProfile = { ...regData, username: normalizedUsername };

            
            
            await saveProfileToSupabase(data.user.id, userProfile);

            showToast(t('registerSuccess'), 'success');
            closeModal('authModal');
            updateAuthUI();
            initializeSocket();
            showHome();
        } else {
            
            userProfile = { ...regData, username: normalizedUsername };
            
            
            const pendingProfile = {
                ...regData,
                username: normalizedUsername,
                pendingUserId: data.user?.id,
                createdAt: new Date().toISOString()
            };
            localStorage.setItem('ozger_pending_profile', JSON.stringify(pendingProfile));
            console.log('Pending profile saved to localStorage for email confirmation');

            showToast('Регистрация успешна! Проверьте вашу почту для подтверждения.', 'success');
            closeModal('authModal');
        }

    } catch (error) {
        showToast(t('registerError') + ': ' + error.message, 'error');
    }
}

async function handleAuth(isLogin) {
    let emailOrUsername = document.getElementById('authEmail')?.value.trim();
    const password = document.getElementById('authPassword')?.value;
    const confirmPassword = document.getElementById('authPasswordConfirm')?.value;
    const username = document.getElementById('authUsername')?.value.trim();
    
    if (!emailOrUsername || !password) {
        showToast(t('fillAllFields'), 'warning');
        return;
    }
    
    if (!isLogin) {
        if (!username) {
            showToast(t('fillAllFields'), 'warning');
            return;
        }
        if (password !== confirmPassword) {
            showToast(t('passwordMismatch'), 'warning');
            return;
        }
    }
    
    if (!supabaseClient) {
        const ready = await ensureSupabaseReady();
        if (!ready) {
            showToast('Supabase not configured', 'error');
            return;
        }
    }
    
    try {
        let email = emailOrUsername;
        
        if (isLogin) {
            
            if (!emailOrUsername.includes('@')) {
                const normalizedUsername = emailOrUsername.toLowerCase();
                
                const { data: profileData, error: lookupError } = await supabaseClient
                    .from('profiles')
                    .select('email')
                    .eq('username', normalizedUsername)
                    .single();
                
                if (lookupError || !profileData?.email) {
                    showToast(t('loginError') + ': ' + t('userNotFound'), 'error');
                    return;
                }
                
                email = profileData.email;
            }
            
            const { data, error } = await supabaseClient.auth.signInWithPassword({ email, password });
            if (error) throw error;
            
            currentUser = data.user;
            userProfile = null;
            userAvatar = null;
            updateAvatarUI(null);
            
            
            const loadedProfile = await loadProfileFromSupabase(data.user.id);
            
            
            if (!loadedProfile || !loadedProfile.school || !loadedProfile.class) {
                const pendingProfileStr = localStorage.getItem('ozger_pending_profile');
                if (pendingProfileStr) {
                    try {
                        const pendingProfile = JSON.parse(pendingProfileStr);
                        console.log('Found pending profile in localStorage:', pendingProfile);
                        
                        
                        userProfile = {
                            username: pendingProfile.username,
                            country: pendingProfile.country,
                            city: pendingProfile.city,
                            school: pendingProfile.school,
                            class: pendingProfile.class,
                            subjectCombination: pendingProfile.subjectCombination,
                            subject1: pendingProfile.subject1,
                            subject2: pendingProfile.subject2,
                            email: data.user.email
                        };
                        
                        const saved = await saveProfileToSupabase(data.user.id, userProfile);
                        if (saved) {
                            console.log('Pending profile synced to Supabase successfully');
                            localStorage.removeItem('ozger_pending_profile');
                        }
                    } catch (e) {
                        console.error('Error parsing pending profile:', e);
                        localStorage.removeItem('ozger_pending_profile');
                    }
                }
            }
            
            
            showToast(t('loginSuccess'), 'success');
            closeModal('authModal');
            updateAuthUI();

            
            initializeSocket();

            
            setTimeout(() => {
                if (userProfile && userProfile.school && userProfile.class) {
                    joinClassroom();
                }
                updateClassroomInfo();
                renderProfilePage();
            }, 500);
        } else {
            const { data, error } = await supabaseClient.auth.signUp({ 
                email, 
                password,
                options: { data: { username: username } }
            });
            if (error) throw error;
            
            showToast(t('registerSuccess'), 'success');
            closeModal('authModal');
        }
    } catch (err) {
        showToast(`${isLogin ? t('loginError') : t('registerError')}: ${err.message}`, 'error');
    }
}

async function handleLogout() {
    if (!supabaseClient) {
        const ready = await ensureSupabaseReady();
        if (!ready) return;
    }

    try {
        await supabaseClient.auth.signOut();
        currentUser = null;
        userProfile = null;
        userAvatar = null;

        
        disconnectSocket();

        closeModal('profileModal');
        closeSidePanelRight();
        updateAuthUI();
        sessionStorage.setItem('ozger_post_logout_reload', '1');
        window.location.assign(window.location.pathname);
    } catch (err) {
        showToast('Logout error: ' + err.message, 'error');
    }
}
async function handleForgotPassword() {
    const emailInput = document.getElementById('resetEmail');
    if (!emailInput || !supabaseClient) return;

    const email = emailInput.value.trim();
    
    if (!email) {
        showToast(t('fillAllFields'), 'warning');
        return;
    }

    const resetUrl = `${window.location.origin}${window.location.pathname}?type=recovery`;
    try {
        
        const { data, error } = await supabaseClient.auth.resetPasswordForEmail(email, {
            redirectTo: resetUrl
        });

        if (error) {
            console.error('Reset password error:', error);
            showToast('Ошибка: ' + error.message, 'error');
            console.log('❌ Проблема при отправке письма!');
            console.log('📧 Проверьте конфигурацию SMTP в Supabase Dashboard');
            console.log('🔧 Убедитесь что в Supabase > Authentication > Email Templates настроены');
            console.log('🔧 И что SMTP сервер настроен в Supabase > Settings > SMTP Settings');
        } else {
            console.log('Письмо для восстановления отправлено:', data);
            showToast('Письмо отправлено на ваш email. Проверьте папку спам.', 'success');
            renderAuthForm('login');
        }
    } catch (err) {
        console.error('Reset password error:', err);
        console.log('❌ Network/Supabase error!');
        console.log('📧 Check SUPABASE_EMAIL_SETUP.md for configuration');
        showToast('Ошибка сети при отправке письма', 'error');



    }
}
window.addEventListener('load', async () => {
    
    await new Promise(resolve => setTimeout(resolve, 100));
    
    await appInitPromise;

    
    const urlParams = new URLSearchParams(window.location.search);
    const hashParams = new URLSearchParams(window.location.hash.substring(1));
    const hasRecoveryToken = urlParams.get('type') === 'recovery' ||
                            hashParams.get('type') === 'recovery' ||
                            window.location.hash.includes('type=recovery');

    if (!hasRecoveryToken) {
        sessionStorage.removeItem('passwordResetMode');
        sessionStorage.removeItem('passwordResetTokens');
    }

    
    

    
    let accessToken = hashParams.get('access_token') || urlParams.get('access_token');
    let refreshToken = hashParams.get('refresh_token') || urlParams.get('refresh_token');
    let type = hashParams.get('type') || urlParams.get('type');

    
    if (!type && window.location.hash.includes('type=recovery')) {
        type = 'recovery';
    }
    if (!type && window.location.hash.includes('type=signup')) {
        type = 'signup';
    }

    
    if (accessToken && refreshToken && type === 'signup') {
        if (!supabaseClient) {
            showToast('Supabase not configured', 'error');
            return;
        }
        console.log('Email confirmation link detected');
        try {
            const { data, error } = await supabaseClient.auth.setSession({
                access_token: accessToken,
                refresh_token: refreshToken
            });

            if (error) {
                console.error('Email confirmation error:', error);
                showToast('Ошибка подтверждения email', 'error');
            } else {
                console.log('Email confirmed successfully');
                currentUser = data.user;
                
                
                const pendingProfileStr = localStorage.getItem('ozger_pending_profile');
                if (pendingProfileStr && data.user) {
                    try {
                        const pendingProfile = JSON.parse(pendingProfileStr);
                        console.log('Found pending profile after email confirmation:', pendingProfile);
                        
                        userProfile = {
                            username: pendingProfile.username,
                            country: pendingProfile.country,
                            city: pendingProfile.city,
                            school: pendingProfile.school,
                            class: pendingProfile.class,
                            subjectCombination: pendingProfile.subjectCombination,
                            subject1: pendingProfile.subject1,
                            subject2: pendingProfile.subject2,
                            email: data.user.email
                        };
                        
                        const saved = await saveProfileToSupabase(data.user.id, userProfile);
                        if (saved) {
                            console.log('Pending profile synced to Supabase after email confirmation');
                        }
                    } catch (e) {
                        console.error('Error syncing pending profile:', e);
                    }
                }
                
                showToast('Email подтвержден! Добро пожаловать!', 'success');
                
                window.history.replaceState(null, null, window.location.pathname);
                updateAuthUI();
                showHome();
            }
        } catch (err) {
            console.error('Error confirming email:', err);
            showToast('Ошибка при подтверждении email', 'error');
        }
    } else if (accessToken && type === 'recovery') {
        
        console.log('Password reset link detected');
        console.log('Current URL:', window.location.href);
        console.log('Hash params:', window.location.hash);
        console.log('Query params:', window.location.search);
        console.log('Access token present:', !!accessToken);
        console.log('Refresh token present:', !!refreshToken);
        console.log('Access token (first 20 chars):', accessToken ? accessToken.substring(0, 20) + '...' : 'null');
        console.log('Type:', type);

        try {
            
            
            const tokenHash = hashParams.get('token_hash') || urlParams.get('token_hash') ||
                            window.location.hash.split('token_hash=')[1]?.split('&')[0];

            console.log('tokenHash from hashParams:', hashParams.get('token_hash'));
            console.log('tokenHash from urlParams:', urlParams.get('token_hash'));
            console.log('tokenHash from hash split:', window.location.hash.split('token_hash=')[1]?.split('&')[0]);
            console.log('Final tokenHash:', tokenHash);
            console.log('Full URL:', window.location.href);
            console.log('Hash part:', window.location.hash);
            console.log('Search part:', window.location.search);

            
            let resetSuccess = false;
            let resetError = null;

            
            if (accessToken && refreshToken) {
                console.log('Trying to set session with access_token and refresh_token');
                try {
                    const { data, error } = await supabaseClient.auth.setSession({
                        access_token: accessToken,
                        refresh_token: refreshToken
                    });

                if (error) {
                    console.log('Session set failed:', error);
                    resetError = error;
                    sessionStorage.removeItem('passwordResetMode');
                    sessionStorage.removeItem('passwordResetTokens');
                    console.log('Password reset flag removed due to session set error');
                } else {
                        console.log('Session set successfully for password reset');
                        resetSuccess = true;

                        
                        window.history.replaceState(null, null, window.location.pathname);

                        
                        sessionStorage.setItem('passwordResetMode', 'true');
                        sessionStorage.setItem('passwordResetTokens', JSON.stringify({
                            access_token: accessToken,
                            refresh_token: refreshToken
                        }));
                        console.log('Password reset flag set to true (setSession approach)');

                        
                        setTimeout(() => checkPasswordResetMode(), 100);
                    }
                } catch (err) {
                    console.log('Session set exception:', err);
                    resetError = err;
                }
            }

            
            if (!resetSuccess && tokenHash) {
                console.log('Trying verifyOtp with token_hash as fallback');
                try {
                    const { data, error } = await supabaseClient.auth.verifyOtp({
                        token_hash: tokenHash,
                        type: 'recovery'
                    });

                    if (error) {
                        console.log('verifyOtp failed:', error);
                        resetError = error;
                    } else {
                        console.log('verifyOtp successful');
                        resetSuccess = true;

                        
                        window.history.replaceState(null, null, window.location.pathname);

                        
                        sessionStorage.setItem('passwordResetMode', 'true');
                        if (accessToken && refreshToken) {
                            sessionStorage.setItem('passwordResetTokens', JSON.stringify({
                                access_token: accessToken,
                                refresh_token: refreshToken
                            }));
                        }
                        console.log('Password reset flag set to true (verifyOtp approach)');

                        
                        setTimeout(() => checkPasswordResetMode(), 100);
                    }
                } catch (err) {
                    console.log('verifyOtp exception:', err);
                    resetError = err;
                    sessionStorage.removeItem('passwordResetMode');
                    sessionStorage.removeItem('passwordResetTokens');
                    console.log('Password reset flag removed due to verifyOtp exception');
                }
            }

            if (resetSuccess) {
                console.log('Password reset flow initiated successfully');
                
            } else {
                console.error('All password reset approaches failed');
                console.error('Final error:', resetError);
                sessionStorage.removeItem('passwordResetMode');
                sessionStorage.removeItem('passwordResetTokens');
                console.log('Password reset flag removed - all approaches failed');
                showToast('Недействительная или истекшая ссылка восстановления', 'error');
            }
        } catch (err) {
            console.error('Error processing reset link:', err);
            
            sessionStorage.removeItem('passwordResetMode');
            sessionStorage.removeItem('passwordResetTokens');
            console.log('Password reset flag removed due to processing error');
            showToast('Ошибка при обработке ссылки восстановления', 'error');
        }
    }
})
async function handleResetPassword() {
    console.log('handleResetPassword called');

    const newPasswordElement = document.getElementById('newPassword');
    const confirmPasswordElement = document.getElementById('confirmNewPassword');

    console.log('newPassword element:', newPasswordElement);
    console.log('confirmPassword element:', confirmPasswordElement);

    const pass1 = newPasswordElement?.value;
    const pass2 = confirmPasswordElement?.value;

    console.log('pass1:', pass1 ? '[HIDDEN]' : 'null/empty');
    console.log('pass2:', pass2 ? '[HIDDEN]' : 'null/empty');

    if (!pass1 || !pass2) {
        console.log('Empty password fields detected');
        const fillAllFieldsText = t('fillAllFields');
        console.log('fillAllFields translation:', fillAllFieldsText);
        showToast(fillAllFieldsText || 'Заполните все поля', 'warning');
        return;
    }

    if (pass1.length < 6) {
        console.log('Password too short:', pass1.length, 'characters');
        showToast('Пароль должен быть минимум 6 символов', 'warning');
        return;
    }

    if (pass1 !== pass2) {
        console.log('Passwords do not match');
        showToast('Пароли не совпадают', 'error');
        return;
    }

    console.log('All validations passed, attempting to update password');

    try {
        let { data: sessionState } = await supabaseClient.auth.getSession();
        if (!sessionState?.session) {
            const storedTokens = sessionStorage.getItem('passwordResetTokens');
            if (storedTokens) {
                try {
                    const parsedTokens = JSON.parse(storedTokens);
                    if (parsedTokens?.access_token && parsedTokens?.refresh_token) {
                        await supabaseClient.auth.setSession({
                            access_token: parsedTokens.access_token,
                            refresh_token: parsedTokens.refresh_token
                        });
                        ({ data: sessionState } = await supabaseClient.auth.getSession());
                    }
                } catch (tokenError) {
                    console.warn('Unable to restore password reset session:', tokenError);
                }
            }
        }

        if (!sessionState?.session) {
            showToast('Session expired. Open reset link again.', 'error');
            return;
        }

        console.log('Calling supabaseClient.auth.updateUser');
        const { data, error } = await supabaseClient.auth.updateUser({
            password: pass1
        });

        console.log('updateUser result:', { data: data ? 'success' : null, error });

        if (error) {
            console.error('updateUser error:', error);
            showToast(error.message, 'error');
            return;
        }

        console.log('Password updated successfully');

        
        console.log('Signing out after password update');
        await supabaseClient.auth.signOut();

        
        sessionStorage.removeItem('passwordResetMode');
        sessionStorage.removeItem('passwordResetTokens');
        console.log('Password reset flag cleared');

        showToast('Пароль успешно обновлен! Вы вошли в аккаунт.', 'success');

        
        console.log('Closing modal and redirecting to home');
        closeModal('authModal');
        showHome();
    } catch (err) {
        console.error('Password update error:', err);
        console.error('Error details:', err.message);
        showToast('Ошибка при обновлении пароля: ' + err.message, 'error');
    }
}


async function loadSession() {
    
    let attempts = 0;
    while (!supabaseClient && attempts < 20) {
        await new Promise(resolve => setTimeout(resolve, 100));
        attempts++;
    }
    
    if (!supabaseClient) {
        console.log('Supabase client not available for session load');
        return;
    }
    
    try {
        const { data, error } = await supabaseClient.auth.getSession();
        if (error) {
            console.error('Error loading session:', error);
            return;
        }
        
        if (data?.session?.user) {
            currentUser = data.session.user;
            console.log('Session restored for user:', currentUser.email);
            
            
            await loadProfileFromSupabase(currentUser.id);
            
            
            await loadUserLikes();
            
            
        }
    } catch (err) {
        console.error('Session load error:', err);
    }
    
    updateAuthUI();
}

function updateAuthUI() {
    const authButtons = document.getElementById('authButtons');
    const userIconBtn = document.getElementById('userIconBtn');
    const userIconText = document.getElementById('userIconText');
    const userAvatarEl = document.getElementById('userAvatar');
    const sidebarUsername = document.getElementById('sidebarUsername');
    const profilePlaceholder = document.getElementById('profilePlaceholder');
    
    if (currentUser) {
        authButtons?.classList.add('hidden');
        userIconBtn?.classList.remove('hidden');
        
        const nameForInitial = userProfile?.username || currentUser.user_metadata?.username || currentUser.email || '?';
        const initial = nameForInitial[0].toUpperCase();
        if (userIconText) userIconText.textContent = initial;
        if (profilePlaceholder) profilePlaceholder.textContent = initial;
        
        if (sidebarUsername) {
            sidebarUsername.textContent = userProfile?.username || currentUser.user_metadata?.username || currentUser.email?.split('@')[0] || t('guest');
        }

        if (userAvatar) {
            updateAvatarUI(userAvatar);
        } else {
            if (userAvatarEl) {
                userAvatarEl.src = '';
                userAvatarEl.classList.add('hidden');
            }
            userIconText?.classList.remove('hidden');
        }
    } else {
        authButtons?.classList.remove('hidden');
        userIconBtn?.classList.add('hidden');
        
        if (sidebarUsername) sidebarUsername.textContent = t('guest');
        if (profilePlaceholder) profilePlaceholder.textContent = '?';
        updateAvatarUI(null);
    }

    updateDailyTestLimitUI();
    updateAIUsageUI();
}


function checkPasswordResetMode() {
    const passwordResetFlag = sessionStorage.getItem('passwordResetMode');
    console.log('checkPasswordResetMode called, flag value:', passwordResetFlag);

    const isPasswordResetMode = passwordResetFlag === 'true';
    if (isPasswordResetMode) {
        console.log('Password reset mode detected, opening reset modal');

        try {
            
            const authModal = document.getElementById('authModal');
            const authFormContainer = document.getElementById('authFormContainer');

            console.log('authModal found:', !!authModal);
            console.log('authFormContainer found:', !!authFormContainer);

            if (!authModal || !authFormContainer) {
                console.warn('DOM elements not ready, retrying in 500ms');
                console.log('Available elements:', {
                    authModal: document.getElementById('authModal'),
                    authFormContainer: document.getElementById('authFormContainer')
                });
                setTimeout(checkPasswordResetMode, 500);
                return;
            }

            console.log('Calling renderAuthForm with reset');
            renderAuthForm('reset');

            
            const newPasswordField = document.getElementById('newPassword');
            const confirmPasswordField = document.getElementById('confirmNewPassword');
            console.log('newPassword field found:', !!newPasswordField);
            console.log('confirmNewPassword field found:', !!confirmPasswordField);

            console.log('Calling openModal with authModal');
            openModal('authModal');

            console.log('Calling showToast');
            showToast('Введите новый пароль для входа в аккаунт', 'info');

            
            sessionStorage.removeItem('passwordResetMode');
            console.log('Password reset flag cleared after successful modal open');
        } catch (err) {
            console.error('Error opening password reset modal:', err);
            sessionStorage.removeItem('passwordResetMode');
            console.log('Password reset flag removed due to modal opening error');
        }
    } else {
        console.log('Password reset mode not detected');
    }
}

// ==================== PROFILE ====================
function openProfileModal() {
    if (!currentUser) {
        openAuthModal('login');
        return;
    }
    
    const profileUsername = document.getElementById('profileUsername');
    const profileEmail = document.getElementById('profileEmail');
    
    if (profileUsername) {
        profileUsername.textContent = currentUser.user_metadata?.username || currentUser.email?.split('@')[0] || '-';
    }
    if (profileEmail) {
        profileEmail.textContent = currentUser.email || '-';
    }
    
    openModal('profileModal');
}

async function handleAvatarChange(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    if (isAvatarSaving) return;
    isAvatarSaving = true;
    
    const reader = new FileReader();
    reader.onerror = () => {
        showToast(t('avatarSaveError') || 'Avatar save failed', 'error');
        isAvatarSaving = false;
    };
    reader.onload = async (event) => {
        try {
            const base64 = reader.result;
            if (typeof base64 !== 'string' || !base64.startsWith('data:')) {
                showToast(t('avatarSaveError') || 'Avatar save failed', 'error');
                isAvatarSaving = false;
                return;
            }

            
            let savedToSupabase = false;
            if (!currentUser) {
                await ensureSessionLoaded();
            }
            if (!currentUser) {
                showToast(t('pleaseLogin') || 'Please login', 'warning');
                isAvatarSaving = false;
                return;
            }

            const profileSaved = await saveAvatarToSupabase(base64);
            savedToSupabase = profileSaved === true;

            if (!savedToSupabase) {
                showToast(t('avatarSaveError') || 'Avatar save failed', 'error');
                isAvatarSaving = false;
                return;
            }

            userAvatar = base64;
            if (!userProfile) userProfile = {};
            userProfile.avatar_url = base64;

            updateAvatarUI(base64);
            updateAuthUI();
            showToast(t('avatarSaved') || t('avatarUpdated'), 'success');
        } catch (err) {
            console.warn('Avatar upload error:', err);
            showToast(t('avatarSaveError') || 'Avatar save failed', 'error');
        } finally {
            if (e?.target) e.target.value = '';
            isAvatarSaving = false;
        }
    };
    reader.readAsDataURL(file);
}

function updateAvatarUI(url) {
    const userAvatarEl = document.getElementById('userAvatar');
    const profileAvatar = document.getElementById('profileAvatar');
    const pageAvatarImg = document.getElementById('pageAvatarImg');
    const pageAvatarPlaceholder = document.getElementById('pageAvatarPlaceholder');
    const userIconText = document.getElementById('userIconText');
    const profilePlaceholder = document.getElementById('profilePlaceholder');

    if (!url) {
        if (userAvatarEl) {
            userAvatarEl.src = '';
            userAvatarEl.classList.add('hidden');
        }
        if (profileAvatar) {
            profileAvatar.src = '';
            profileAvatar.classList.add('hidden');
        }
        if (pageAvatarImg) {
            pageAvatarImg.src = '';
            pageAvatarImg.classList.add('hidden');
        }
        userIconText?.classList.remove('hidden');
        profilePlaceholder?.classList.remove('hidden');
        pageAvatarPlaceholder?.classList.remove('hidden');

        const deleteBtn = document.getElementById('deleteAvatarBtn');
        if (deleteBtn) deleteBtn.classList.add('hidden');
        return;
    }
    
    if (userAvatarEl) {
        userAvatarEl.src = url;
        userAvatarEl.classList.remove('hidden');
        userIconText?.classList.add('hidden');
    }
    
    if (profileAvatar) {
        profileAvatar.src = url;
        profileAvatar.classList.remove('hidden');
        profilePlaceholder?.classList.add('hidden');
    }
    
    if (pageAvatarImg) {
        pageAvatarImg.src = url;
        pageAvatarImg.classList.remove('hidden');
        if (pageAvatarPlaceholder) pageAvatarPlaceholder.classList.add('hidden');
    }
    
    
    const deleteBtn = document.getElementById('deleteAvatarBtn');
    if (deleteBtn) {
        deleteBtn.classList.toggle('hidden', !url);
    }

    
    const classmatesPage = document.getElementById('classmatesPage');
    if (classmatesPage && !classmatesPage.classList.contains('hidden')) {
        renderClassmates();
    }
}

async function deleteAvatar() {
    userAvatar = null;
    
    
    if (currentUser && supabaseClient) {
        try {
            await saveAvatarToSupabase(null);
        } catch (err) {
            console.warn('Could not delete avatar from server:', err);
        }
    }
    
    
    const userAvatarEl = document.getElementById('userAvatar');
    const profileAvatar = document.getElementById('profileAvatar');
    const modalAvatarImg = document.getElementById('modalAvatarImg');
    const pageAvatarImg = document.getElementById('pageAvatarImg');
    
    if (userAvatarEl) {
        userAvatarEl.src = '';
        userAvatarEl.classList.add('hidden');
        document.getElementById('userIconText')?.classList.remove('hidden');
    }
    
    if (profileAvatar) {
        profileAvatar.src = '';
        profileAvatar.classList.add('hidden');
        document.getElementById('profilePlaceholder')?.classList.remove('hidden');
    }
    
    if (modalAvatarImg) {
        modalAvatarImg.src = '';
        modalAvatarImg.classList.add('hidden');
        document.getElementById('modalAvatarPlaceholder')?.classList.remove('hidden');
    }
    
    if (pageAvatarImg) {
        pageAvatarImg.src = '';
        pageAvatarImg.classList.add('hidden');
        document.getElementById('pageAvatarPlaceholder')?.classList.remove('hidden');
    }
    
    
    const deleteBtn = document.getElementById('deleteAvatarBtn');
    if (deleteBtn) {
        deleteBtn.classList.add('hidden');
    }
    
    updateAuthUI();
    showToast(t('avatarDeleted'), 'success');
}

// ==================== INPUT SECTION ====================
function clearInput() {
    const input = document.getElementById('materialInput');
    if (input) input.value = '';
    hideError();
}

function loadSampleMaterial() {
    const input = document.getElementById('materialInput');
    if (input) input.value = sampleMaterial;
}

function runCorrector() {
    const input = document.getElementById('materialInput');
    if (!input) return;
    
    const raw = input.value.trim();
    if (!raw) return;
    
    const lines = raw.split('\n').map(l => l.trim()).filter(Boolean);
    const merged = [];
    let buffer = '';
    const numbered = /^\d+[\.\)]/;

    lines.forEach(line => {
        const isNumbered = numbered.test(line);

        if (isNumbered && buffer) {
            merged.push(buffer);
            buffer = line;
        } else if (isNumbered && !buffer) {
            buffer = line;
        } else {
            buffer = buffer ? `${buffer} ${line}` : line;
        }
    });

    if (buffer) merged.push(buffer);

    const fixed = merged.map((line, idx) => {
        let text = line.replace(/^\d+[\.\)]\s*/, '').trim();
        if (!text.includes(':')) {
            const dashSplit = text.split(/[-–—]/);
            if (dashSplit.length >= 2) {
                text = dashSplit[0].trim() + ': ' + dashSplit.slice(1).join('-').trim();
            }
        } else {
            const colonIndex = text.indexOf(':');
            const question = text.substring(0, colonIndex).trim();
            const answer = text.substring(colonIndex + 1).trim().replace(/\s+/g, ' ');
            text = `${question}: ${answer}`;
        }
        return `${idx + 1}. ${text}`;
    }).join('\n');

    input.value = fixed;
}

function showError(message) {
    const errorDiv = document.getElementById('errorMessage');
    if (errorDiv) {
        errorDiv.textContent = message;
        errorDiv.classList.remove('hidden');
    }
}

function hideError() {
    const errorDiv = document.getElementById('errorMessage');
    if (errorDiv) errorDiv.classList.add('hidden');
}

function parseInput(material) {
    const lines = material.split('\n').filter(line => line.trim());
    const facts = [];

    lines.forEach((line) => {
        let cleanLine = line.replace(/^\d+[\.\)]\s*/, '').trim();
        const colonIndex = cleanLine.lastIndexOf(':');
        if (colonIndex > 0 && colonIndex < cleanLine.length - 1) {
            const question = cleanLine.substring(0, colonIndex).trim();
            const answer = cleanLine.substring(colonIndex + 1).trim();
            
            if (question && answer) {
                facts.push({
                    index: facts.length + 1,
                    question: question,
                    answer: answer,
                    original: line.trim()
                });
            }
        }
    });
    
    return facts;
}

function showModuleSelection() {
    const material = document.getElementById('materialInput')?.value.trim();
    
    if (!material) {
        showError(t('errorEmpty'));
        return;
    }
    
    hideError();
    factsData = parseInput(material);
    
    if (factsData.length === 0) {
        showError(t('errorFormat'));
        return;
    }
    
    openModal('moduleModal');
}

// ==================== LEARNING ====================
function startLearning() {
    enabledModules.flashcards = document.getElementById('chkFlashcards')?.checked || false;
    enabledModules.quiz = document.getElementById('chkQuiz')?.checked || false;
    enabledModules.matching = document.getElementById('chkMatching')?.checked || false;
    
    if (!enabledModules.flashcards && !enabledModules.quiz && !enabledModules.matching) {
        showToast(t('errorSelectModule'), 'warning');
        return;
    }
    
    closeModal('moduleModal');
    
    sectionScores = {
        flashcards: { correct: 0, total: 0, answered: 0 },
        quiz: { correct: 0, total: 0, answered: 0 },
        matching: { correct: 0, total: 0, answered: 0 }
    };
    
    currentModule = -1;
    score = 0;
    totalQuestions = 0;
    matchedPairs = [];
    
    showLearning();
    nextModule();
}

function getEnabledModulesList() {
    const modules = [];
    let num = 1;
    if (enabledModules.flashcards) modules.push({ id: 'flashcardsModule', titleKey: 'flashcards', init: initFlashcards, key: 'flashcards', num: num++ });
    if (enabledModules.quiz) modules.push({ id: 'quizModule', titleKey: 'quiz', init: initQuiz, key: 'quiz', num: num++ });
    if (enabledModules.matching) modules.push({ id: 'matchingModule', titleKey: 'matching', init: initMatching, key: 'matching', num: num++ });
    return modules;
}

function showModule(moduleIndex) {
    const modules = getEnabledModulesList();
    
    if (moduleIndex < 0 || moduleIndex >= modules.length) {
        currentModule = modules.length;
        document.querySelectorAll('.learning-module').forEach(m => m.classList.remove('active'));
        document.getElementById('completionModule')?.classList.add('active');
        document.getElementById('moduleTitle').textContent = t('resultsTitle');
        document.getElementById('prevModuleBtn').style.display = 'none';
        document.getElementById('nextModuleBtn').style.display = 'none';
        document.getElementById('finishBtn').style.display = 'none';
        showCompletion();
        return;
    }
    
    document.querySelectorAll('.learning-module').forEach(m => m.classList.remove('active'));
    currentModule = moduleIndex;
    const moduleInfo = modules[moduleIndex];
    document.getElementById('moduleTitle').textContent = `${moduleInfo.num}. ${t(moduleInfo.titleKey)}`;
    document.getElementById(moduleInfo.id)?.classList.add('active');
    updateProgress();
    updateScoreDisplay();
    updateModuleNavigation();
    moduleInfo.init();
}

function updateProgress() {
    const modules = getEnabledModulesList();
    const progress = modules.length > 0 ? Math.min(((currentModule + 1) / modules.length) * 100, 100) : 0;
    const progressBar = document.getElementById('progressBar');
    if (progressBar) {
        progressBar.style.width = progress + '%';
        progressBar.textContent = Math.round(progress) + '%';
    }
}

function updateScoreDisplay() {
    document.getElementById('scoreValue').textContent = score;
    document.getElementById('totalValue').textContent = totalQuestions;
    const percent = totalQuestions > 0 ? Math.round((score / totalQuestions) * 100) : 0;
    document.getElementById('percentValue').textContent = percent + '%';
}

function updateModuleNavigation() {
    const modules = getEnabledModulesList();
    const prevModuleBtn = document.getElementById('prevModuleBtn');
    const nextModuleBtn = document.getElementById('nextModuleBtn');
    const finishBtn = document.getElementById('finishBtn');
    
    if (currentModule >= modules.length) {
        if (prevModuleBtn) prevModuleBtn.style.display = 'none';
        if (nextModuleBtn) nextModuleBtn.style.display = 'none';
        if (finishBtn) finishBtn.style.display = 'none';
        return;
    }
    
    if (prevModuleBtn) prevModuleBtn.style.display = currentModule <= 0 ? 'none' : 'inline-flex';
    if (nextModuleBtn) nextModuleBtn.style.display = (currentModule >= modules.length - 1) ? 'none' : 'inline-flex';
    if (finishBtn) finishBtn.style.display = 'inline-flex';
}

function previousModule() {
    if (currentModule > 0) {
        showModule(currentModule - 1);
    }
}

function nextModule() {
    const modules = getEnabledModulesList();
    if (currentModule < modules.length - 1) {
        showModule(currentModule + 1);
    } else {
        showModule(modules.length);
    }
}

function finishAndShowResults() {
    const modules = getEnabledModulesList();
    showModule(modules.length);
}

function confirmExitLearning() {
    if (confirm(t('exitConfirm'))) {
        showHome();
    }
}

// ==================== FLASHCARDS ====================
function initFlashcards() {
    currentCard = 0;
    sectionScores.flashcards = { correct: 0, total: factsData.length, answered: 0 };
    showFlashcard(0);
}

function showFlashcard(index) {
    if (index < 0 || index >= factsData.length) return;
    
    const container = document.getElementById('flashcardContainer');
    const fact = factsData[index];
    
    document.getElementById('cardCounter').textContent = `${index + 1} / ${factsData.length}`;
    updateCardNavigation();
    
    
    container.innerHTML = '';

    const wrapper = document.createElement('div');
    wrapper.className = 'flashcard-wrapper';

    const flashcard = document.createElement('div');
    flashcard.className = 'flashcard';
    flashcard.id = 'currentFlashcard';
    flashcard.onclick = flipCard;

    const frontFace = document.createElement('div');
    frontFace.className = 'flashcard-face flashcard-front';

    const question = document.createElement('div');
    question.className = 'flashcard-question';
    question.textContent = `${fact.question}:`;

    const frontHint = document.createElement('div');
    frontHint.className = 'flashcard-hint';
    frontHint.textContent = t('flashcardHint');

    const backFace = document.createElement('div');
    backFace.className = 'flashcard-face flashcard-back';

    const answer = document.createElement('div');
    answer.className = 'flashcard-answer';
    answer.textContent = fact.answer;

    const backHint = document.createElement('div');
    backHint.className = 'flashcard-hint';
    backHint.textContent = t('flashcardBackHint');

    frontFace.appendChild(question);
    frontFace.appendChild(frontHint);

    backFace.appendChild(answer);
    backFace.appendChild(backHint);

    flashcard.appendChild(frontFace);
    flashcard.appendChild(backFace);
    wrapper.appendChild(flashcard);

    const scoring = document.createElement('div');
    scoring.className = 'flashcard-scoring';
    scoring.id = 'flashcardScoring';
    scoring.style.display = 'none';

    const knewBtn = document.createElement('button');
    knewBtn.className = 'score-btn knew';
    knewBtn.textContent = t('flashcardKnew');
    knewBtn.onclick = () => scoreFlashcard(true);

    const didntKnowBtn = document.createElement('button');
    didntKnowBtn.className = 'score-btn didnt-know';
    didntKnowBtn.textContent = t('flashcardDidntKnow');
    didntKnowBtn.onclick = () => scoreFlashcard(false);

    scoring.appendChild(knewBtn);
    scoring.appendChild(didntKnowBtn);

    container.appendChild(wrapper);
    container.appendChild(scoring);
}

function updateCardNavigation() {
    const prevBtn = document.getElementById('prevCardBtn');
    const nextBtn = document.getElementById('nextCardBtn');
    
    if (prevBtn) prevBtn.style.display = currentCard <= 0 ? 'none' : 'inline-flex';
    if (nextBtn) nextBtn.style.display = currentCard >= factsData.length - 1 ? 'none' : 'inline-flex';
}

function flipCard() {
    const card = document.getElementById('currentFlashcard');
    if (card) {
        card.classList.toggle('flipped');
        const scoringDiv = document.getElementById('flashcardScoring');
        if (scoringDiv) {
            scoringDiv.style.display = card.classList.contains('flipped') ? 'flex' : 'none';
        }
    }
}

function scoreFlashcard(knew) {
    totalQuestions++;
    sectionScores.flashcards.answered++;
    if (knew) {
        score++;
        sectionScores.flashcards.correct++;
    }
    updateScoreDisplay();
    
    if (currentCard < factsData.length - 1) {
        currentCard++;
        showFlashcard(currentCard);
    } else {
        document.getElementById('flashcardContainer').innerHTML = `
            <div style="text-align: center; padding: 40px;">
                <div style="font-size: 3em; margin-bottom: 15px;">🎉</div>
                <h3 style="color: var(--color-primary);">${t('allCardsDone')}</h3>
                <p style="color: var(--text-muted); margin-top: 10px;">${t('goNextModule')}</p>
            </div>
        `;
        document.getElementById('prevCardBtn').style.display = 'none';
        document.getElementById('nextCardBtn').style.display = 'none';
    }
}

function previousCard() {
    if (currentCard > 0) {
        currentCard--;
        showFlashcard(currentCard);
    }
}

function nextCard() {
    if (currentCard < factsData.length - 1) {
        currentCard++;
        showFlashcard(currentCard);
    }
}

// ==================== QUIZ ====================
function initQuiz() {
    const container = document.getElementById('quizContainer');
    container.innerHTML = '';
    
    sectionScores.quiz = { correct: 0, total: factsData.length, answered: 0 };
    totalQuestions += factsData.length;

    factsData.forEach((fact, index) => {
        const questionBox = document.createElement('div');
        questionBox.className = 'question-box';
        questionBox.dataset.answered = 'false';
        
        const options = generateQuizOptions(fact, index);
        
        const h3 = document.createElement('h3');
        h3.textContent = `${t('quizQuestion')} ${index + 1}`;

        const questionText = document.createElement('div');
        questionText.className = 'question-text';
        questionText.textContent = `${fact.question}:`;

        const optionsDiv = document.createElement('div');
        optionsDiv.className = 'options';
        optionsDiv.id = `options-${index}`;

        options.forEach((opt, i) => {
            const option = document.createElement('div');
            option.className = 'option';
            option.textContent = opt;
            option.onclick = () => checkQuizAnswer(index, i, fact.answer);
            optionsDiv.appendChild(option);
        });

        questionBox.appendChild(h3);
        questionBox.appendChild(questionText);
        questionBox.appendChild(optionsDiv);
        
        container.appendChild(questionBox);
    });
    
    updateScoreDisplay();
}

function generateQuizOptions(fact, factIndex) {
    const correctAnswer = fact.answer;
    const options = [correctAnswer];
    
    let attempts = 0;
    while (options.length < 4 && attempts < 50) {
        const randomFact = factsData[Math.floor(Math.random() * factsData.length)];
        if (randomFact.answer !== correctAnswer && !options.includes(randomFact.answer)) {
            options.push(randomFact.answer);
        }
        attempts++;
    }
    
    return shuffleArray(options);
}

function checkQuizAnswer(questionIndex, optionIndex, correctAnswer) {
    const optionsContainer = document.getElementById(`options-${questionIndex}`);
    if (!optionsContainer) return;
    
    const questionBox = optionsContainer.closest('.question-box');
    if (questionBox.dataset.answered === 'true') return;
    questionBox.dataset.answered = 'true';
    
    const options = optionsContainer.querySelectorAll('.option');
    const selectedOption = options[optionIndex];
    const selectedText = selectedOption.textContent.trim();
    
    options.forEach(option => {
        option.classList.add('disabled');
        if (option.textContent.trim() === correctAnswer) {
            option.classList.add('correct');
        }
    });
    
    sectionScores.quiz.answered++;
    
    if (selectedText === correctAnswer) {
        score++;
        sectionScores.quiz.correct++;
    } else {
        selectedOption.classList.add('incorrect');
    }
    
    updateScoreDisplay();
}

// ==================== MATCHING ====================
function initMatching() {
    matchedPairs = [];
    selectedMatchItem = null;
    
    sectionScores.matching = { correct: 0, total: factsData.length, answered: 0 };
    totalQuestions += factsData.length;
    
    renderMatching();
    updateScoreDisplay();
}

function renderMatching() {
    const container = document.getElementById('matchingContainer');
    const unmatchedFacts = factsData.filter(f => !matchedPairs.includes(f.index));
    
    
    container.innerHTML = '';

    const title = document.createElement('h3');
    title.textContent = t('matchingTitle');
    container.appendChild(title);

    if (matchedPairs.length > 0) {
        const matchedPairsDiv = document.createElement('div');
        matchedPairsDiv.className = 'matched-pairs';

        matchedPairs.forEach(factIndex => {
            const fact = factsData.find(f => f.index === factIndex);
            const matchedPair = document.createElement('div');
            matchedPair.className = 'matched-pair';

            const questionSide = document.createElement('div');
            questionSide.className = 'question-side';
            questionSide.textContent = fact.question;

            const answerSide = document.createElement('div');
            answerSide.className = 'answer-side';
            answerSide.textContent = fact.answer;

            matchedPair.appendChild(questionSide);
            matchedPair.appendChild(answerSide);
            matchedPairsDiv.appendChild(matchedPair);
        });

        container.appendChild(matchedPairsDiv);
    }

    if (unmatchedFacts.length > 0) {
        const shuffledQuestions = shuffleArray([...unmatchedFacts]);
        const shuffledAnswers = shuffleArray([...unmatchedFacts]);

        const matchingGame = document.createElement('div');
        matchingGame.className = 'matching-game';

        
        const questionsColumn = document.createElement('div');
        questionsColumn.className = 'matching-column';

        const questionsTitle = document.createElement('h4');
        questionsTitle.textContent = t('matchingQuestions');
        questionsColumn.appendChild(questionsTitle);

        shuffledQuestions.forEach(fact => {
            const item = document.createElement('div');
            item.className = 'matching-item';
            item.dataset.factIndex = fact.index;
            item.dataset.side = 'left';
            item.textContent = fact.question;
            item.onclick = () => selectMatchItem(item);
            questionsColumn.appendChild(item);
        });

        
        const answersColumn = document.createElement('div');
        answersColumn.className = 'matching-column';

        const answersTitle = document.createElement('h4');
        answersTitle.textContent = t('matchingAnswers');
        answersColumn.appendChild(answersTitle);

        shuffledAnswers.forEach(fact => {
            const item = document.createElement('div');
            item.className = 'matching-item';
            item.dataset.factIndex = fact.index;
            item.dataset.side = 'right';
            item.textContent = fact.answer;
            item.onclick = () => selectMatchItem(item);
            answersColumn.appendChild(item);
        });

        matchingGame.appendChild(questionsColumn);
        matchingGame.appendChild(answersColumn);
        container.appendChild(matchingGame);
    } else {
        const completionDiv = document.createElement('div');
        completionDiv.style.textAlign = 'center';
        completionDiv.style.padding = '30px';

        const trophy = document.createElement('div');
        trophy.style.fontSize = '3em';
        trophy.style.marginBottom = '15px';
        trophy.textContent = '🎉';

        const completionTitle = document.createElement('h3');
        completionTitle.style.color = 'var(--color-primary)';
        completionTitle.textContent = t('allMatched');

        completionDiv.appendChild(trophy);
        completionDiv.appendChild(completionTitle);
        container.appendChild(completionDiv);
    }
}

function selectMatchItem(item) {
    const side = item.dataset.side;
    const factIndex = item.dataset.factIndex;
    
    if (!selectedMatchItem) {
        item.classList.add('selected');
        selectedMatchItem = { element: item, side, factIndex };
    } else if (selectedMatchItem.side === side) {
        selectedMatchItem.element.classList.remove('selected');
        item.classList.add('selected');
        selectedMatchItem = { element: item, side, factIndex };
    } else {
        if (selectedMatchItem.factIndex === factIndex) {
            matchedPairs.push(parseInt(factIndex));
            score++;
            sectionScores.matching.correct++;
            sectionScores.matching.answered++;
            updateScoreDisplay();
            
            setTimeout(() => {
                renderMatching();
            }, 300);
        } else {
            item.classList.add('wrong');
            selectedMatchItem.element.classList.add('wrong');
            
            setTimeout(() => {
                item.classList.remove('wrong');
                selectedMatchItem.element.classList.remove('wrong', 'selected');
                selectedMatchItem = null;
            }, 500);
            return;
        }
        selectedMatchItem = null;
    }
}

// ==================== COMPLETION ====================
function showCompletion() {
    const statsContainer = document.getElementById('completionStats');
    
    let totalCorrect = 0;
    let totalAll = 0;
    if (enabledModules.flashcards) { totalCorrect += sectionScores.flashcards.correct; totalAll += sectionScores.flashcards.total; }
    if (enabledModules.quiz) { totalCorrect += sectionScores.quiz.correct; totalAll += sectionScores.quiz.total; }
    if (enabledModules.matching) { totalCorrect += sectionScores.matching.correct; totalAll += sectionScores.matching.total; }
    
    const sectionInfo = [
        { key: 'flashcards', name: t('flashcards'), icon: '📇', enabled: enabledModules.flashcards },
        { key: 'quiz', name: t('quiz'), icon: '✅', enabled: enabledModules.quiz },
        { key: 'matching', name: t('matching'), icon: '🔗', enabled: enabledModules.matching }
    ];
    
    const gradeClass = (pct) => pct >= 80 ? 'excellent' : pct >= 60 ? 'good' : pct >= 40 ? 'average' : 'poor';

    
    statsContainer.innerHTML = '';

    const sectionResults = document.createElement('div');
    sectionResults.className = 'section-results';

    const title = document.createElement('h3');
    title.textContent = t('resultsBySection');
    sectionResults.appendChild(title);

    sectionInfo.forEach(section => {
        if (!section.enabled) return;
        const data = sectionScores[section.key];
        const pct = data.total > 0 ? Math.round((data.correct / data.total) * 100) : 0;
        const g = gradeClass(pct);

        const resultItem = document.createElement('div');
        resultItem.className = 'section-result-item';

        const sectionIcon = document.createElement('div');
        sectionIcon.className = 'section-icon';
        sectionIcon.textContent = section.icon;

        const sectionInfoDiv = document.createElement('div');
        sectionInfoDiv.className = 'section-info';

        const sectionName = document.createElement('div');
        sectionName.className = 'section-name';
        sectionName.textContent = section.name;

        const sectionScore = document.createElement('div');
        sectionScore.className = 'section-score';
        sectionScore.textContent = `${data.correct} / ${data.total} ${t('correctWord')}`;

        const sectionProgress = document.createElement('div');
        sectionProgress.className = 'section-progress';

        const progressFill = document.createElement('div');
        progressFill.className = `section-progress-fill ${g}`;
        progressFill.style.width = `${pct}%`;

        const sectionPercent = document.createElement('div');
        sectionPercent.className = `section-percent ${g}`;
        sectionPercent.textContent = `${pct}%`;

        sectionProgress.appendChild(progressFill);
        sectionInfoDiv.appendChild(sectionName);
        sectionInfoDiv.appendChild(sectionScore);
        sectionInfoDiv.appendChild(sectionProgress);

        resultItem.appendChild(sectionIcon);
        resultItem.appendChild(sectionInfoDiv);
        resultItem.appendChild(sectionPercent);

        sectionResults.appendChild(resultItem);
    });

    statsContainer.appendChild(sectionResults);

    
    const totalPercent = totalAll > 0 ? Math.round((totalCorrect / totalAll) * 100) : 0;

    
    sendUserActivity('test_completed', {
        score: totalPercent,
        subject: currentSubject,
        totalQuestions: totalAll
    });

    
    checkAchievementsAfterTest(totalPercent, totalAll);
}

// ==================== LIBRARY ====================
function setLibraryFilter(filterType, value) {
    if (filterType === 'owner') {
        currentLibraryTab = value;
        
        document.querySelectorAll('.library-filters .filter-tabs:first-child .filter-tab').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.filter === value);
        });
        
        
        const visibilityTabs = document.querySelector('.library-filters .filter-tabs:last-child');
        if (visibilityTabs) {
            visibilityTabs.style.display = value === 'my' ? 'flex' : 'none';
        }
        
        
        if (value !== 'my') {
            currentVisibilityFilter = 'all';
        }
    } else if (filterType === 'visibility') {
        currentVisibilityFilter = value;
        
        document.querySelectorAll('.library-filters .filter-tabs:last-child .filter-tab').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.filter === value);
        });
    } else if (filterType === 'type') {
        currentLibraryType = value;
        document.querySelectorAll('.filter-tab[data-type]').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.type === value);
        });
    }
    renderLibrary();
}

function filterBySubject(subject) {
    if (subject) {
        currentSubjectFilter = subject;
        const select = document.getElementById('subjectFilter');
        if (select) select.value = subject;
    } else {
        currentSubjectFilter = document.getElementById('subjectFilter')?.value || 'all';
    }
    renderLibrary();
}

function switchLibraryTab(tab) {
    currentLibraryTab = tab;
    document.querySelectorAll('.filter-tab[data-filter]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.filter === tab);
    });
    renderLibrary();
}

function getAllMaterials() {
    
    return [...libraryMaterials, ...userMaterials];
}


let supabaseTests = [];
let supabaseTestsLoaded = false;
let supabaseTestsRequestPromise = null;
let supabaseTestsLastLoadedAt = 0;
const SUPABASE_TESTS_CACHE_TTL_MS = 30 * 1000;

async function loadTestsFromSupabase(forceRefresh = false) {
    if (!supabaseClient) return [];

    const cacheAge = Date.now() - supabaseTestsLastLoadedAt;
    if (!forceRefresh && supabaseTestsLoaded && cacheAge < SUPABASE_TESTS_CACHE_TTL_MS) {
        return supabaseTests;
    }

    if (!forceRefresh && supabaseTestsRequestPromise) {
        return supabaseTestsRequestPromise;
    }

    supabaseTestsRequestPromise = (async () => {
        let userId = currentUser?.id;
        if (!userId) {
            try {
                const { data: sessionData } = await supabaseClient.auth.getSession();
                userId = sessionData?.session?.user?.id;
            } catch { }
        }

        const supabaseUrl = supabaseClient.supabaseUrl;
        const supabaseKey = supabaseClient.supabaseKey;

        const rows = await fetchJsonWithControl(`${supabaseUrl}/rest/v1/tests?select=*&order=created_at.desc`, {
            headers: {
                'apikey': supabaseKey,
                'Authorization': `Bearer ${supabaseKey}`,
                'Content-Type': 'application/json'
            },
            timeoutMs: 6000,
            cacheKey: 'supabase-tests:all',
            cacheTtlMs: 10 * 1000
        });

        const data = Array.isArray(rows) ? rows : [];
        supabaseTests = data.map(t => {
            let questionsRaw = t.questions;
            try {
                if (typeof questionsRaw === 'string') {
                    questionsRaw = JSON.parse(questionsRaw);
                }
            } catch {
                questionsRaw = [];
            }

            const normalized = normalizeQuestionsPayload(questionsRaw);
            const isOwn = userId && t.user_id === userId;

            return {
                id: t.id,
                supabase_id: t.id,
                title: t.title,
                subject: t.subject,
                category: t.subject,
                type: 'test',
                is_public: t.is_public,
                isPublic: t.is_public,
                content: t.content,
                questions: normalized.items || [],
                question_order: normalized.order,
                answer_mode: normalized.answerMode,
                hints_enabled: normalized.hintsEnabled,
                author: t.author || 'Anonymous',
                author_id: t.user_id,
                isOwn: isOwn,
                isUserMaterial: isOwn,
                created_at: t.created_at,
                count: t.count || (normalized.items?.length || 0)
            };
        });

        supabaseTestsLoaded = true;
        supabaseTestsLastLoadedAt = Date.now();
        return supabaseTests;
    })().catch(error => {
        console.error('Error loading tests:', error);
        return [];
    }).finally(() => {
        supabaseTestsRequestPromise = null;
    });

    return supabaseTestsRequestPromise;
}

async function renderLibrary() {
    const grid = document.getElementById('libraryGrid');
    const emptyState = document.getElementById('emptyLibrary');
    if (!grid) {
        console.error('Library grid not found');
        return;
    }
    const shouldLoadRemoteTests = supabaseClient &&
        (!supabaseTestsLoaded || (Date.now() - supabaseTestsLastLoadedAt) > SUPABASE_TESTS_CACHE_TTL_MS);

    if (shouldLoadRemoteTests) {
        grid.innerHTML = `<div class="loading-state">${t('loading')}...</div>`;
        try {
            await loadTestsFromSupabase();
        } catch (e) {
            console.error('Error loading tests:', e);
        }
    }

    let currentUserId = currentUser?.id;
    if (!currentUserId && supabaseClient) {
        try {
            const { data: sessionData } = await supabaseClient.auth.getSession();
            currentUserId = sessionData?.session?.user?.id;
        } catch { }
    }

    const testsWithOwnership = supabaseTests.map(t => ({
        ...t,
        isOwn: currentUserId && t.author_id === currentUserId,
        isUserMaterial: currentUserId && t.author_id === currentUserId
    }));

    const visibilityFilter = currentVisibilityFilter || 'all';

    let materials;
    if (currentLibraryTab === 'my') {
        materials = testsWithOwnership.filter(t => t.isOwn);
        if (visibilityFilter === 'public') {
            materials = materials.filter(m => m.is_public);
        } else if (visibilityFilter === 'private') {
            materials = materials.filter(m => !m.is_public);
        }
    } else {
        materials = testsWithOwnership.filter(t => t.is_public || t.isOwn);
    }

    if (currentSubjectFilter && currentSubjectFilter !== 'all') {
        materials = materials.filter(m => m.category === currentSubjectFilter || m.subject === currentSubjectFilter);
    }

    const query = document.getElementById('librarySearch')?.value.toLowerCase() || '';
    if (query) {
        materials = materials.filter(m => 
            m.title?.toLowerCase().includes(query) ||
            m.author?.toLowerCase().includes(query) ||
            (m.category && m.category.toLowerCase().includes(query))
        );
    }

    materials.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    
    if (materials.length === 0) {
        grid.innerHTML = '';
        if (emptyState) emptyState.classList.remove('hidden');
        return;
    }
    
    if (emptyState) emptyState.classList.add('hidden');
    
   
    grid.innerHTML = '';

    materials.forEach(material => {
        const isFavorite = favorites.includes(material.id);
        
        const isOwn = material.isOwn || material.isUserMaterial || 
                      (currentUserId && material.author_id === currentUserId);
        const categorySvg = getCategoryIcon(normalizeSubjectKey(material.category || material.subject), true);

        const card = document.createElement('div');
        card.className = 'material-card';
        
        card.onclick = () => {
            if (material.questions && material.questions.length > 0) {
                showTestDetailsModal(material);
            } else {
                openQuicklook(material.id, isOwn);
            }
        };

        const header = document.createElement('div');
        header.className = 'material-card-header';

        const title = document.createElement('div');
        title.className = 'material-card-title';
        title.textContent = material.title;

        const badge = document.createElement('div');
        badge.className = 'material-card-badge material-badge-svg';
        badge.innerHTML = categorySvg;

        header.appendChild(title);
        header.appendChild(badge);

        const meta = document.createElement('div');
        meta.className = 'material-card-meta';

        const authorSpan = document.createElement('span');
        authorSpan.className = 'meta-item-svg';
        authorSpan.innerHTML = `<svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg> ${material.author || t('anonymous')}`;

        
        const subjectKey = material.subject || material.category || 'other';
        const subjectIconKey = normalizeSubjectKey(subjectKey);
        const subjectName = getSubjectDisplayName(subjectKey);
        const subjectSpan = document.createElement('span');
        subjectSpan.className = 'meta-item-svg meta-subject';
        subjectSpan.innerHTML = `${getCategoryIcon(subjectIconKey, true)} ${subjectName}`;

        const countSpan = document.createElement('span');
        countSpan.className = 'meta-item-svg';
        const questionCount = material.count || material.questions?.length || 0;
        countSpan.innerHTML = `<svg viewBox="0 0 24 24"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg> ${questionCount} ${t('questions')}`;

        meta.appendChild(authorSpan);
        meta.appendChild(subjectSpan);
        meta.appendChild(countSpan);

        const actions = document.createElement('div');
        actions.className = 'material-card-actions';
        actions.onclick = (e) => e.stopPropagation();

        const useBtn = document.createElement('button');
        useBtn.className = 'card-action-btn action-btn-svg';
        useBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg> ${t('useMaterial')}`;
        useBtn.onclick = (e) => { e.stopPropagation(); useMaterial(material.id, isOwn); };

        actions.appendChild(useBtn);

        
        const starBtn = document.createElement('button');
        starBtn.className = `card-action-btn action-btn-svg favorite-btn ${isFavorite ? 'favorite-active' : ''}`;
        starBtn.setAttribute('data-test-id', material.id);
        starBtn.title = isFavorite ? t('removeFromFavorites') : t('addToFavorites');
        starBtn.innerHTML = isFavorite
            ? '<svg viewBox="0 0 24 24"><path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>'
            : '<svg viewBox="0 0 24 24"><path d="M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z"/></svg>';
        starBtn.onclick = (e) => { e.stopPropagation(); toggleFavoriteWithUI(material.id, !!material.isUserMaterial, starBtn); };
        actions.appendChild(starBtn);

        if (isOwn) {
            if (!material.is_public) {
                const publishBtn = document.createElement('button');
                publishBtn.className = 'card-action-btn action-btn-svg publish-btn';
                publishBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M5 20h14v-2H5v2zm7-18l-5.5 5.5 1.42 1.42L11 6.83V16h2V6.83l3.08 3.09 1.42-1.42L12 2z"/></svg>' + ` ${t('publish')}`;
                publishBtn.onclick = (e) => { e.stopPropagation(); publishTest(material.id); };
                actions.appendChild(publishBtn);
            }

            const deleteBtn = document.createElement('button');
            deleteBtn.className = 'card-action-btn delete-btn action-btn-svg';
            deleteBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>';
            deleteBtn.onclick = (e) => { e.stopPropagation(); showDeleteConfirm(material.id); };
            actions.appendChild(deleteBtn);
        }

        card.appendChild(header);
        card.appendChild(meta);
        card.appendChild(actions);

        grid.appendChild(card);
    });
}

function normalizeSubjectKey(subjectKey) {
    if (!subjectKey) return 'other';
    if (subjectKey === 'history_kz') return 'history';
    if (subjectKey === 'world_history') return 'history';
    return subjectKey;
}

function getCategoryIcon(category, asSvg = false) {
    if (asSvg) {
        const svgIcons = {
            history: '<svg viewBox="0 0 24 24"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>',
            history_kz: '<svg viewBox="0 0 24 24"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>',
            world_history: '<svg viewBox="0 0 24 24"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>',
            math: '<svg viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-5 14h-2v-4H8v-2h4V7h2v4h4v2h-4v4z"/></svg>',
            physics: '<svg viewBox="0 0 24 24"><path d="M19.8 18.4L14 10.67V6.5l1.35-1.69c.26-.33.03-.81-.39-.81H9.04c-.42 0-.65.48-.39.81L10 6.5v4.17L4.2 18.4c-.49.66-.02 1.6.8 1.6h14c.82 0 1.29-.94.8-1.6z"/></svg>',
            science: '<svg viewBox="0 0 24 24"><path d="M19.8 18.4L14 10.67V6.5l1.35-1.69c.26-.33.03-.81-.39-.81H9.04c-.42 0-.65.48-.39.81L10 6.5v4.17L4.2 18.4c-.49.66-.02 1.6.8 1.6h14c.82 0 1.29-.94.8-1.6z"/></svg>',
            biology: '<svg viewBox="0 0 24 24"><path d="M19.8 18.4L14 10.67V6.5l1.35-1.69c.26-.33.03-.81-.39-.81H9.04c-.42 0-.65.48-.39.81L10 6.5v4.17L4.2 18.4c-.49.66-.02 1.6.8 1.6h14c.82 0 1.29-.94.8-1.6z"/></svg>',
            chemistry: '<svg viewBox="0 0 24 24"><path d="M19.8 18.4L14 10.67V6.5l1.35-1.69c.26-.33.03-.81-.39-.81H9.04c-.42 0-.65.48-.39.81L10 6.5v4.17L4.2 18.4c-.49.66-.02 1.6.8 1.6h14c.82 0 1.29-.94.8-1.6z"/></svg>',
            informatics: '<svg viewBox="0 0 24 24"><path d="M20 18c1.1 0 1.99-.9 1.99-2L22 6c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2H0v2h24v-2h-4zM4 6h16v10H4V6z"/></svg>',
            language: '<svg viewBox="0 0 24 24"><path d="M12.87 15.07l-2.54-2.51.03-.03A17.52 17.52 0 0 0 14.07 6H17V4h-7V2H8v2H1v2h11.17C11.5 7.92 10.44 9.75 9 11.35 8.07 10.32 7.3 9.19 6.69 8h-2c.73 1.63 1.73 3.17 2.98 4.56l-5.09 5.02L4 19l5-5 3.11 3.11.76-2.04zM18.5 10h-2L12 22h2l1.12-3h4.75L21 22h2l-4.5-12zm-2.62 7l1.62-4.33L19.12 17h-3.24z"/></svg>',
            kazakh: '<svg viewBox="0 0 24 24"><path d="M12.87 15.07l-2.54-2.51.03-.03A17.52 17.52 0 0 0 14.07 6H17V4h-7V2H8v2H1v2h11.17C11.5 7.92 10.44 9.75 9 11.35 8.07 10.32 7.3 9.19 6.69 8h-2c.73 1.63 1.73 3.17 2.98 4.56l-5.09 5.02L4 19l5-5 3.11 3.11.76-2.04zM18.5 10h-2L12 22h2l1.12-3h4.75L21 22h2l-4.5-12zm-2.62 7l1.62-4.33L19.12 17h-3.24z"/></svg>',
            russian: '<svg viewBox="0 0 24 24"><path d="M12.87 15.07l-2.54-2.51.03-.03A17.52 17.52 0 0 0 14.07 6H17V4h-7V2H8v2H1v2h11.17C11.5 7.92 10.44 9.75 9 11.35 8.07 10.32 7.3 9.19 6.69 8h-2c.73 1.63 1.73 3.17 2.98 4.56l-5.09 5.02L4 19l5-5 3.11 3.11.76-2.04zM18.5 10h-2L12 22h2l1.12-3h4.75L21 22h2l-4.5-12zm-2.62 7l1.62-4.33L19.12 17h-3.24z"/></svg>',
            english: '<svg viewBox="0 0 24 24"><path d="M12.87 15.07l-2.54-2.51.03-.03A17.52 17.52 0 0 0 14.07 6H17V4h-7V2H8v2H1v2h11.17C11.5 7.92 10.44 9.75 9 11.35 8.07 10.32 7.3 9.19 6.69 8h-2c.73 1.63 1.73 3.17 2.98 4.56l-5.09 5.02L4 19l5-5 3.11 3.11.76-2.04zM18.5 10h-2L12 22h2l1.12-3h4.75L21 22h2l-4.5-12zm-2.62 7l1.62-4.33L19.12 17h-3.24z"/></svg>',
            geography: '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>',
            law: '<svg viewBox="0 0 24 24"><path d="M12 2l3 5h-6l3-5zm0 7c-2.21 0-4 1.79-4 4v7h8v-7c0-2.21-1.79-4-4-4zm-7 1h2v10H5V10zm12 0h2v10h-2V10z"/></svg>',
            other: '<svg viewBox="0 0 24 24"><path d="M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H8V4h12v12zM10 9h8v2h-8zm0 3h4v2h-4zm0-6h8v2h-8z"/></svg>'
        };
        return svgIcons[category] || svgIcons.other;
    }
    
    const icons = {
        history: '📜',
        math: '🔢',
        physics: '⚛️',
        science: '🔬',
        biology: '🧬',
        chemistry: '🧪',
        informatics: '💻',
        language: '🌍',
        kazakh: '🇰🇿',
        russian: '🇷🇺',
        english: '🇬🇧',
        geography: '🗺️',
        other: '📚'
    };
    return icons[category] || '📚';
}


function getSubjectName(subjectKey) {
    const subjectNames = {
        kk: {
            history: 'Қазақстан тарихы',
            math: 'Математика',
            physics: 'Физика',
            science: 'Жаратылыстану',
            biology: 'Биология',
            chemistry: 'Химия',
            informatics: 'Информатика',
            language: 'Тілдер',
            kazakh: 'Қазақ тілі',
            russian: 'Орыс тілі',
            english: 'Ағылшын тілі',
            geography: 'География',
            other: 'Басқа'
        },
        ru: {
            history: 'История Казахстана',
            math: 'Математика',
            physics: 'Физика',
            science: 'Естествознание',
            biology: 'Биология',
            chemistry: 'Химия',
            informatics: 'Информатика',
            language: 'Языки',
            kazakh: 'Казахский язык',
            russian: 'Русский язык',
            english: 'Английский язык',
            geography: 'География',
            other: 'Другое'
        },
        en: {
            history: 'History of Kazakhstan',
            math: 'Mathematics',
            physics: 'Physics',
            science: 'Science',
            biology: 'Biology',
            chemistry: 'Chemistry',
            informatics: 'Informatics',
            language: 'Languages',
            kazakh: 'Kazakh Language',
            russian: 'Russian Language',
            english: 'English Language',
            geography: 'Geography',
            other: 'Other'
        }
    };
    
    const lang = currentLang || 'kk';
    return subjectNames[lang]?.[subjectKey] || subjectNames[lang]?.other || subjectKey;
}

function getSubjectDisplayName(subjectKey) {
    const lang = currentLang || 'kk';
    const overrides = {
        history_kz: {
            kk: 'Қазақстан тарихы',
            ru: 'История Казахстана',
            en: 'History of Kazakhstan'
        },
        world_history: {
            kk: 'Дүние жүзі тарихы',
            ru: 'Всемирная история',
            en: 'World History'
        },
        law: {
            kk: 'Құқық негіздері',
            ru: 'Основы права',
            en: 'Law'
        }
    };

    if (overrides[subjectKey]?.[lang]) {
        return overrides[subjectKey][lang];
    }

    return getSubjectName(subjectKey);
}

function useMaterial(id, isUserMaterial = false) {
    
    let material = supabaseTests.find(m => m.id === id || m.supabase_id === id);
    
    if (!material) {
        const materials = isUserMaterial ? userMaterials : getAllMaterials();
        material = materials.find(m => m.id === id);
    }
    
    if (!material) {
        material = userTests.find(m => m.id === id);
    }
    
    if (material) {
        
        if (material.questions && material.questions.length > 0) {
            startTestFromLibrary(material);
        } else if (material.content) {
            
            document.getElementById('materialInput').value = material.content;
            showInputSection();
        }
    }
}

async function toggleFavoriteLocal(id, isUserMaterial = false) {
    if (!currentUser) {
        await ensureSessionLoaded();
    }
    if (!currentUser) {
        showToast(t('pleaseLogin'), 'warning');
        return;
    }
    
    const testId = id; 
    const index = favorites.indexOf(testId);
    const wasInFavorites = index > -1;
    
    
    if (wasInFavorites) {
        favorites.splice(index, 1);
        removeFromFavoritesSupabase(testId);
    } else {
        favorites.push(testId);
        addToFavoritesSupabase(testId);
    }
    
    renderLibrary();
    renderFavorites();
}


async function toggleFavoriteWithUI(id, isUserMaterial, buttonEl) {
    if (!currentUser) {
        await ensureSessionLoaded();
    }
    if (!currentUser) {
        showToast(t('pleaseLogin'), 'warning');
        return;
    }
    
    const testId = id;
    const index = favorites.indexOf(testId);
    const wasInFavorites = index > -1;
    
    
    if (wasInFavorites) {
        favorites.splice(index, 1);
        removeFromFavoritesSupabase(testId);
    } else {
        favorites.push(testId);
        addToFavoritesSupabase(testId);
    }
    
    const isNowFavorite = !wasInFavorites;
    
    
    if (buttonEl) {
        buttonEl.classList.toggle('favorite-active', isNowFavorite);
        buttonEl.innerHTML = isNowFavorite 
            ? '<svg viewBox="0 0 24 24"><path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>' 
            : '<svg viewBox="0 0 24 24"><path d="M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z"/></svg>';
        buttonEl.title = isNowFavorite ? t('removeFromFavorites') : t('addToFavorites');
    }
    
    
    document.querySelectorAll(`.favorite-btn[data-test-id="${id}"]`).forEach(btn => {
        if (btn !== buttonEl) {
            btn.classList.toggle('favorite-active', isNowFavorite);
            btn.innerHTML = isNowFavorite 
                ? '<svg viewBox="0 0 24 24"><path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>' 
                : '<svg viewBox="0 0 24 24"><path d="M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z"/></svg>';
        }
    });
    
    
    updateTestDetailFavoriteBtnUI(id, isNowFavorite);

    showToast(wasInFavorites ? t('removedFromFavorites') : t('addedToFavorites'), 'success');
    renderFavorites();
}


function updateTestDetailFavoriteBtnUI(testId, isFavorite) {
    const btn = document.getElementById('testDetailFavoriteBtn');
    const icon = document.getElementById('testDetailFavIcon');
    if (btn && icon && pendingTestData?.id === testId) {
        btn.classList.toggle('active', isFavorite);
        icon.innerHTML = isFavorite 
            ? '<path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/>'
            : '<path d="M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z"/>';
    }
}

// ==================== LIKES SYSTEM (removed) ====================

async function loadUserLikes() {  }
async function toggleLike() {  }
async function toggleLikeWithUI() {  }
function updateAllLikeButtons() {  }
async function updateTestLikesCount() {  }

function searchLibrary() {
    renderLibrary();
}

// ==================== UPLOAD MATERIAL ====================
function showUploadPage() {
    hideAllPages();
    document.getElementById('uploadPage')?.classList.remove('hidden');
    
    document.getElementById('uploadTitle').value = '';
    document.getElementById('uploadContent').value = '';
    document.getElementById('uploadCategory').value = 'other';
    document.getElementById('uploadPublic').checked = true;
}

function uploadMaterial() {
    const title = document.getElementById('uploadTitle')?.value.trim();
    const content = document.getElementById('uploadContent')?.value.trim();
    const category = document.getElementById('uploadCategory')?.value || 'other';
    const isPublic = document.getElementById('uploadPublic')?.checked || false;
    
    if (!title || !content) {
        showToast(t('fillTitleContent'), 'warning');
        return;
    }
    
    
    const lines = content.split('\n').filter(line => line.trim());
    const count = lines.length;
    
    
    const newMaterial = {
        id: Date.now(),
        title: title,
        author: currentUser ? (currentUser.user_metadata?.username || currentUser.email?.split('@')[0]) : t('guest'),
        count: count,
        content: content,
        category: category,
        isPublic: isPublic,
        isUserMaterial: true,
        createdAt: new Date().toISOString()
    };
    
    userMaterials.push(newMaterial);
    
    showToast(t('materialUploaded'), 'success');
    showLibrary();
    switchLibraryTab('my');
}

// ==================== QUICKLOOK ====================
function openQuicklook(id, isUserMaterial = false) {
    const materials = isUserMaterial ? userMaterials : getAllMaterials();
    const material = materials.find(m => m.id === id);
    if (!material) return;
    
    quicklookMaterial = { ...material, isUserMaterial };
    
    
    document.getElementById('quicklookTitle').textContent = material.title;
    document.getElementById('quicklookAuthor').textContent = material.author;
    document.getElementById('quicklookCount').textContent = `${material.count} ${t('questions')}`;
    document.getElementById('quicklookIcon').innerHTML = getCategoryIcon(material.category, true);
    document.getElementById('quicklookCategory').textContent = t(`cat${capitalize(material.category || 'Other')}`);
    
    
    const previewContainer = document.getElementById('quicklookPreview');
    const parsed = parseInput(material.content);
    const previewItems = parsed.slice(0, 5);
    
    
    previewContainer.innerHTML = '';

    const previewList = document.createElement('div');
    previewList.className = 'preview-list';

    previewItems.forEach((item, index) => {
        const previewItem = document.createElement('div');
        previewItem.className = 'preview-item';

        const numberDiv = document.createElement('div');
        numberDiv.className = 'preview-number';
        numberDiv.textContent = `${index + 1}.`;

        const contentDiv = document.createElement('div');
        contentDiv.className = 'preview-content';

        const questionDiv = document.createElement('div');
        questionDiv.className = 'preview-question';
        questionDiv.textContent = item.question;

        const answerDiv = document.createElement('div');
        answerDiv.className = 'preview-answer';
        answerDiv.textContent = `→ ${item.answer}`;

        contentDiv.appendChild(questionDiv);
        contentDiv.appendChild(answerDiv);

        previewItem.appendChild(numberDiv);
        previewItem.appendChild(contentDiv);

        previewList.appendChild(previewItem);
    });

    if (parsed.length > 5) {
        const moreDiv = document.createElement('div');
        moreDiv.className = 'preview-more';
        moreDiv.textContent = `... ${t('andMore')} ${parsed.length - 5} ${t('questions')}`;
        previewList.appendChild(moreDiv);
    }

    previewContainer.appendChild(previewList);
    
    
    updateQuicklookFavoriteBtn();
    
    openModal('quicklookModal');
}

function updateQuicklookFavoriteBtn() {
    if (!quicklookMaterial) return;
    const favId = quicklookMaterial.isUserMaterial ? `user_${quicklookMaterial.id}` : quicklookMaterial.id;
    const isFavorite = favorites.includes(favId);
    
    const favIcon = document.getElementById('quicklookFavIcon');
    const favBtn = document.getElementById('quicklookFavoriteBtn');
    if (favIcon) favIcon.textContent = isFavorite ? '⭐' : '☆';
    if (favBtn) {
        favBtn.querySelector('[data-i18n]')?.setAttribute('data-i18n', isFavorite ? 'removeFromFavorites' : 'addToFavorites');
        const textEl = favBtn.querySelector('[data-i18n]');
        if (textEl) textEl.textContent = t(isFavorite ? 'removeFromFavorites' : 'addToFavorites');
    }
}

function toggleQuicklookFavorite() {
    if (!quicklookMaterial) return;
    toggleFavorite(quicklookMaterial.id, quicklookMaterial.isUserMaterial);
    updateQuicklookFavoriteBtn();
}

function useQuicklookMaterial() {
    if (!quicklookMaterial) return;
    closeModal('quicklookModal');
    document.getElementById('materialInput').value = quicklookMaterial.content;
    showInputSection();
}

function capitalize(str) {
    return str.charAt(0).toUpperCase() + str.slice(1);
}

// ==================== PUBLISH TEST ====================
async function publishTest(testId) {
    if (!supabaseClient || !currentUser) {
        showToast(t('pleaseLogin'), 'warning');
        return;
    }

    try {
        const { error } = await supabaseClient
            .from('tests')
            .update({ is_public: true })
            .eq('id', testId)
            .eq('user_id', currentUser.id);

        if (error) {
            console.error('Publish test error:', error);
            showToast(t('testPublishError'), 'error');
            return;
        }

        const testEntry = supabaseTests.find(t => t.id === testId);
        if (testEntry) {
            testEntry.is_public = true;
            testEntry.isPublic = true;
        }

        showToast(t('testPublished'), 'success');
        renderLibrary();
    } catch (error) {
        console.error('Publish test error:', error);
        showToast(t('testPublishError'), 'error');
    }
}

// ==================== DELETE MATERIAL ====================
function showDeleteConfirm(id) {
    deleteTargetId = id;
    openModal('confirmDeleteModal');
}

async function confirmDeleteMaterial() {
    if (deleteTargetId === null) return;
    
    
    const localIndex = userMaterials.findIndex(m => m.id === deleteTargetId);
    if (localIndex > -1) {
        userMaterials.splice(localIndex, 1);
    }
    
    
    const supabaseIndex = supabaseTests.findIndex(t => t.id === deleteTargetId);
    if (supabaseIndex > -1 && supabaseClient && currentUser) {
        try {
            const { error } = await supabaseClient
                .from('tests')
                .delete()
                .eq('id', deleteTargetId)
                .eq('user_id', currentUser.id);

            if (error) {
                console.error('Failed to delete from Supabase:', error);
            } else {
                
                supabaseTests.splice(supabaseIndex, 1);
                console.log('Test deleted from Supabase:', deleteTargetId);
            }
        } catch (error) {
            console.error('Error deleting from Supabase:', error);
        }
    }
    
    
    const favIds = [deleteTargetId, `user_${deleteTargetId}`];
    favIds.forEach(favId => {
        const favIndex = favorites.indexOf(favId);
        if (favIndex > -1) {
            favorites.splice(favIndex, 1);
            
            removeFromFavoritesSupabase(deleteTargetId);
        }
    });
    
    showToast(t('materialDeleted'), 'success');
    renderLibrary();
    renderFavorites();
    
    deleteTargetId = null;
    closeModal('confirmDeleteModal');
}

// ==================== FAVORITES ====================
function renderFavorites() {
    const grid = document.getElementById('favoritesGrid');
    const emptyState = document.getElementById('emptyFavorites');
    if (!grid || !emptyState) return;
    
    
    const favoriteMaterials = [];
    
    favorites.forEach(favId => {
        if (typeof favId === 'string' && favId.startsWith('user_')) {
            const id = parseInt(favId.replace('user_', ''));
            const material = userMaterials.find(m => m.id === id);
            if (material) favoriteMaterials.push({ ...material, isUserMaterial: true });
        } else {
            
            let material = supabaseTests.find(m => m.id === favId);
            if (material) {
                favoriteMaterials.push({ ...material, isUserMaterial: false });
            } else {
                material = libraryMaterials.find(m => m.id === favId);
                if (material) favoriteMaterials.push({ ...material, isUserMaterial: false });
            }
        }
    });
    
    if (favoriteMaterials.length === 0) {
        grid.innerHTML = '';
        emptyState.classList.remove('hidden');
        return;
    }
    
    emptyState.classList.add('hidden');
    
    grid.innerHTML = '';

    favoriteMaterials.forEach(material => {
        const categorySvg = getCategoryIcon(normalizeSubjectKey(material.category || material.subject), true);

        const card = document.createElement('div');
        card.className = 'material-card';
        card.onclick = () => openQuicklook(material.id, material.isUserMaterial);

        const header = document.createElement('div');
        header.className = 'material-card-header';

        const title = document.createElement('div');
        title.className = 'material-card-title';
        title.textContent = material.title;

        const badge = document.createElement('div');
        badge.className = 'material-card-badge material-badge-svg';
        badge.innerHTML = categorySvg;

        header.appendChild(title);
        header.appendChild(badge);

        const meta = document.createElement('div');
        meta.className = 'material-card-meta';

        const authorSpan = document.createElement('span');
        authorSpan.className = 'meta-item-svg';
        authorSpan.innerHTML = `<svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg> ${material.author}`;

        const countSpan = document.createElement('span');
        countSpan.className = 'meta-item-svg';
        const questionCount = material.count || material.questions?.length || 0;
        countSpan.innerHTML = `<svg viewBox="0 0 24 24"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg> ${questionCount} ${t('questions')}`;

        meta.appendChild(authorSpan);
        meta.appendChild(countSpan);

        const actions = document.createElement('div');
        actions.className = 'material-card-actions';
        actions.onclick = (e) => e.stopPropagation();

        const useBtn = document.createElement('button');
        useBtn.className = 'card-action-btn action-btn-svg';
        useBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg> ${t('useMaterial')}`;
        useBtn.onclick = (e) => { e.stopPropagation(); useMaterial(material.id, material.isUserMaterial); };

        const favoriteBtn = document.createElement('button');
        favoriteBtn.className = 'card-action-btn action-btn-svg favorite-active';
        favoriteBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>';
        favoriteBtn.onclick = (e) => { e.stopPropagation(); toggleFavorite(material.id, material.isUserMaterial); };

        actions.appendChild(useBtn);
        actions.appendChild(favoriteBtn);

        card.appendChild(header);
        card.appendChild(meta);
        card.appendChild(actions);

        grid.appendChild(card);
    });
}

// ==================== UTILITY FUNCTIONS ====================
function shuffleArray(array) {
    const newArray = [...array];
    for (let i = newArray.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [newArray[i], newArray[j]] = [newArray[j], newArray[i]];
    }
    return newArray;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML.replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

// ==================== EVENT LISTENERS ====================
const debouncedLibrarySearch = debounce(() => searchLibrary(), 220);

function initEventListeners() {
    
    document.getElementById('startBtn')?.addEventListener('click', handleStartBtn);
    
    
    document.getElementById('hamburgerBtn')?.addEventListener('click', openSidePanelLeft);
    document.getElementById('closePanelLeft')?.addEventListener('click', closeSidePanelLeft);
    
    
    document.getElementById('userIconBtn')?.addEventListener('click', openSidePanelRight);
    document.getElementById('closePanelRight')?.addEventListener('click', closeSidePanelRight);
    
    
    document.getElementById('loginBtn')?.addEventListener('click', () => openAuthModal('login'));
    document.getElementById('registerBtn')?.addEventListener('click', () => openAuthModal('register'));
    
    
    document.getElementById('changeStyleBtn')?.addEventListener('click', () => {
        closeSidePanelLeft();
        openStyleModal();
    });
    document.getElementById('changeLangBtn')?.addEventListener('click', () => {
        closeSidePanelLeft();
        document.querySelectorAll('.lang-card').forEach(card => {
            card.classList.toggle('active', card.dataset.lang === currentLang);
        });
        openModal('langModal');
    });
    document.getElementById('faqBtn')?.addEventListener('click', () => {
        closeSidePanelLeft();
        renderFaqContent();
        openModal('faqModal');
    });
    
    
    document.querySelectorAll('.style-card').forEach(card => {
        card.addEventListener('click', () => {
            selectStyle(card.dataset.style);
        });
    });
    document.getElementById('applyStyleBtn')?.addEventListener('click', applySelectedStyle);
    
    
    document.getElementById('profileBtn')?.addEventListener('click', () => {
        closeSidePanelRight();
        showProfile();
    });
    
    
    document.getElementById('myMaterialsBtn')?.addEventListener('click', () => {
        closeSidePanelRight();
        showLibrary();
        setTimeout(() => setLibraryFilter('owner', 'my'), 100);
    });
    
    
    document.getElementById('favoritesMenuBtn')?.addEventListener('click', () => {
        closeSidePanelRight();
        showFavorites();
    });
    
    
    document.getElementById('guideBtn')?.addEventListener('click', () => {
        closeSidePanelLeft();
        showFaqSection('guide');
        openModal('faqModal');
    });
    
    
    document.getElementById('classmatesBtn')?.addEventListener('click', () => {
        closeSidePanelRight();
        showClassmates();
    });
    
    
    
    
    document.getElementById('logoutMenuBtn')?.addEventListener('click', () => {
        closeSidePanelRight();
        handleLogout();
    });
    
    
    document.querySelectorAll('.lang-card').forEach(card => {
        card.addEventListener('click', () => {
            setLanguage(card.dataset.lang);
            showToast(t('languageChanged'), 'success');
            closeModal('langModal');
        });
    });
    
    
    document.getElementById('changeAvatarBtn')?.addEventListener('click', () => {
        document.getElementById('avatarInput')?.click();
    });
    document.getElementById('avatarInput')?.addEventListener('change', handleAvatarChange);
    
    
    document.getElementById('librarySearch')?.addEventListener('keyup', (e) => {
        if (e.key === 'Enter') {
            debouncedLibrarySearch.cancel();
            searchLibrary();
        }
    });
    document.getElementById('librarySearch')?.addEventListener('input', () => {
        debouncedLibrarySearch();
    });
    
    
    document.getElementById('blurOverlay')?.addEventListener('click', () => {
        closeAllSidePanels();
        closeAllModals();
    });
    
    
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeAllSidePanels();
            closeAllModals();
        }
    });

    
}

// ==================== ACHIEVEMENTS ====================
const achievements = [
    { id: 'first_test', nameKey: 'achievementFirstTest', descKey: 'achievementFirstTestDesc', icon: '🎯', unlocked: false },
    { id: 'perfect_score', nameKey: 'achievementPerfectScore', descKey: 'achievementPerfectScoreDesc', icon: '💎', unlocked: false },
    { id: 'excellent_score', nameKey: 'achievementExcellentScore', descKey: 'achievementExcellentScoreDesc', icon: '🏆', unlocked: false },
    { id: 'material_creator', nameKey: 'achievementMaterialCreator', descKey: 'achievementMaterialCreatorDesc', icon: '📝', unlocked: false },
    { id: 'social_butterfly', nameKey: 'achievementSocialButterfly', descKey: 'achievementSocialButterflyDesc', icon: '🦋', unlocked: false },
    { id: 'dedicated_learner', nameKey: 'achievementDedicatedLearner', descKey: 'achievementDedicatedLearnerDesc', icon: '📚', unlocked: false },
    { id: 'early_bird', nameKey: 'achievementEarlyBird', descKey: 'achievementEarlyBirdDesc', icon: '🌅', unlocked: false },
    { id: 'night_owl', nameKey: 'achievementNightOwl', descKey: 'achievementNightOwlDesc', icon: '🦉', unlocked: false }
];

function loadAchievements() {
    const achievementsGrid = document.getElementById('achievementsGrid');
    if (!achievementsGrid) return;

    achievementsGrid.innerHTML = '';

    achievements.forEach(achievement => {
        const card = document.createElement('div');
        card.className = `achievement-card ${achievement.unlocked ? 'unlocked' : 'locked'}`;

        const icon = document.createElement('div');
        icon.className = 'achievement-icon';
        icon.textContent = achievement.icon;

        const name = document.createElement('div');
        name.className = 'achievement-name';
        name.textContent = t(achievement.nameKey);

        const desc = document.createElement('div');
        desc.className = 'achievement-desc';
        desc.textContent = t(achievement.descKey);

        card.appendChild(icon);
        card.appendChild(name);
        card.appendChild(desc);

        achievementsGrid.appendChild(card);
    });
}

function unlockAchievement(achievementId) {
    const achievement = achievements.find(a => a.id === achievementId);
    if (achievement && !achievement.unlocked) {
        achievement.unlocked = true;
        loadAchievements();

        
        sendAchievementUnlocked(t(achievement.nameKey), t(achievement.descKey));

        showToast(`🏆 ${t('achievementUnlocked')}: ${t(achievement.nameKey)}!`, 'success');
    }
}


function checkAchievementsAfterTest(score, totalQuestions) {
    
    unlockAchievement('first_test');

    
    if (score === 100) {
        unlockAchievement('perfect_score');
    }

    
    if (score >= 90) {
        unlockAchievement('excellent_score');
    }

    
    userStats.totalTests = (userStats.totalTests || 0) + 1;
    saveUserStats();

    if (userStats.totalTests >= 10) {
        unlockAchievement('dedicated_learner');
    }
}

// ==================== GUESS GAME ====================
let guessGameData = null;
let currentGuessIndex = 0;
let currentFigure = null;
let currentHintLevel = 1;
let selectedFactsByLevel = {}; 
let shownFactsInRound = []; 
let guessStreak = 0; 
let guessBestStreak = 0; 
const GUESS_MIN_INPUT_LENGTH = 3;
const GUESS_MIN_PART_LENGTH = 4;
const GUESS_FULL_RATIO_THRESHOLD = 0.78;
const GUESS_PARTIAL_RATIO_THRESHOLD = 0.88;
const GUESS_PARTIAL_COVERAGE_THRESHOLD = 0.45;
const GUESS_WORD_RATIO_THRESHOLD = 0.8;

function normalizeGuessText(value) {
    return (value || '')
        .toLowerCase()
        .normalize('NFKD')
        .replace(/[\u0300-\u036f]/g, '')
        .replace(/ё/g, 'е')
        .replace(/[’'`]/g, '')
        .replace(/[^a-z0-9а-яәіңғүұқөһ\s-]/gi, ' ')
        .replace(/[-_]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

function levenshteinDistance(a, b) {
    if (a === b) return 0;
    if (!a.length) return b.length;
    if (!b.length) return a.length;

    const previousRow = Array.from({ length: b.length + 1 }, (_, i) => i);
    const currentRow = new Array(b.length + 1);

    for (let i = 1; i <= a.length; i++) {
        currentRow[0] = i;
        for (let j = 1; j <= b.length; j++) {
            const cost = a[i - 1] === b[j - 1] ? 0 : 1;
            const insertion = currentRow[j - 1] + 1;
            const deletion = previousRow[j] + 1;
            const substitution = previousRow[j - 1] + cost;
            currentRow[j] = Math.min(insertion, deletion, substitution);
        }
        for (let j = 0; j <= b.length; j++) {
            previousRow[j] = currentRow[j];
        }
    }

    return previousRow[b.length];
}

function fuzzyRatio(a, b) {
    if (!a || !b) return 0;
    const maxLength = Math.max(a.length, b.length);
    if (!maxLength) return 1;
    return 1 - (levenshteinDistance(a, b) / maxLength);
}

function fuzzyPartialRatio(a, b) {
    if (!a || !b) return 0;
    let short = a;
    let long = b;
    if (a.length > b.length) {
        short = b;
        long = a;
    }

    if (short.length === long.length) {
        return fuzzyRatio(short, long);
    }

    const windowSize = short.length;
    let best = 0;
    for (let i = 0; i <= long.length - windowSize; i++) {
        const slice = long.slice(i, i + windowSize);
        const score = fuzzyRatio(short, slice);
        if (score > best) best = score;
        if (best === 1) break;
    }
    return best;
}

function isGuessCloseEnough(userInput, correctName) {
    const user = normalizeGuessText(userInput);
    const target = normalizeGuessText(correctName);

    if (!user || !target || user.length < GUESS_MIN_INPUT_LENGTH) return false;
    if (user === target) return true;

    const targetWords = target.split(' ').filter(Boolean);
    if (user.length >= GUESS_MIN_PART_LENGTH && targetWords.some(word => word === user)) {
        return true;
    }

    if (user.length >= GUESS_MIN_PART_LENGTH && targetWords.some(word => fuzzyRatio(user, word) >= GUESS_WORD_RATIO_THRESHOLD)) {
        return true;
    }

    const fullRatio = fuzzyRatio(user, target);
    if (fullRatio >= GUESS_FULL_RATIO_THRESHOLD) {
        return true;
    }

    const coverage = user.length / target.length;
    const partialRatio = fuzzyPartialRatio(user, target);
    return (
        user.length >= GUESS_MIN_PART_LENGTH &&
        coverage >= GUESS_PARTIAL_COVERAGE_THRESHOLD &&
        partialRatio >= GUESS_PARTIAL_RATIO_THRESHOLD
    );
}

async function loadGuessGameData() {
    if (guessGameData) return guessGameData;
    
    try {
        console.log('Loading guess game data...');
        const response = await fetch('data/historical-figures.json');
        console.log('Response status:', response.status);
        if (response.ok) {
            guessGameData = await response.json();
            console.log('Loaded figures:', guessGameData.figures?.length);
            return guessGameData;
        } else {
            console.error('Failed to load game data, status:', response.status);
        }
    } catch (error) {
        console.error('Error loading guess game data:', error);
    }
    return null;
}

function showGuessGame() {
    hideAllPages();
    document.getElementById('guessGamePage')?.classList.remove('hidden');
    
    
    document.getElementById('guessStreak').textContent = guessStreak;
    document.getElementById('guessBestStreak').textContent = guessBestStreak;
    
    
    document.getElementById('guessStartScreen')?.classList.remove('hidden');
    document.getElementById('guessPlayScreen')?.classList.add('hidden');
    document.getElementById('guessResultScreen')?.classList.add('hidden');
}

async function startGuessGame() {
    const data = await loadGuessGameData();
    if (!data || !data.figures || data.figures.length === 0) {
        showToast(t('errorLoadingData') || 'Деректерді жүктеу қатесі', 'error');
        return;
    }
    
    
    
    
    
    pickNewFigure(data);
    
    
    document.getElementById('guessStartScreen')?.classList.add('hidden');
    document.getElementById('guessPlayScreen')?.classList.remove('hidden');
    document.getElementById('guessResultScreen')?.classList.add('hidden');
}

function pickNewFigure(data) {
    const figures = data.figures;
    const randomIndex = Math.floor(Math.random() * figures.length);
    currentFigure = figures[randomIndex];
    currentHintLevel = 1;
    selectedFactsByLevel = {};
    shownFactsInRound = [];

    
    if (currentFigure?.facts && Array.isArray(currentFigure.facts)) {
        [1, 2, 3].forEach(level => {
            const candidates = currentFigure.facts.filter(f => f.level === level);
            if (candidates.length > 0) {
                selectedFactsByLevel[level] = candidates[Math.floor(Math.random() * candidates.length)];
            }
        });
    }
    
    
    showCurrentHint(true);
    
    
    const input = document.getElementById('guessInput');
    if (input) {
        input.value = '';
        input.focus();
    }
}

function showCurrentHint(isFirstHint = false) {
    if (!currentFigure) return;
    
    const container = document.getElementById('guessFactsContainer');
    if (!container) return;
    
    
    if (isFirstHint) {
        container.innerHTML = '';
    }
    
    const fact = selectedFactsByLevel[currentHintLevel] || currentFigure.facts.find(f => f.level === currentHintLevel);
    if (fact) {
        
        if (!shownFactsInRound.find(f => f.level === currentHintLevel)) {
            shownFactsInRound.push(fact);
        }
        const factText = fact.text[currentLang] || fact.text.ru || fact.text.en;
        
        
        const factCard = document.createElement('div');
        factCard.className = 'guess-fact-card fact-level-' + currentHintLevel;
        factCard.setAttribute('data-level', currentHintLevel);
        
        const levelBadge = document.createElement('span');
        levelBadge.className = 'fact-level-badge';
        levelBadge.textContent = t('factLevel') + ' ' + currentHintLevel;
        
        const factP = document.createElement('p');
        factP.className = 'guess-fact-text';
        
        
        const theme = document.body.getAttribute('data-theme') || 'basic';
        
        if (theme === 'flow' && !isFirstHint) {
            
            factP.textContent = '';
            factCard.appendChild(levelBadge);
            factCard.appendChild(factP);
            container.appendChild(factCard);
            animateDecryption(factP, factText);
        } else if (theme === 'pixel' && !isFirstHint) {
            
            factP.textContent = factText;
            factCard.appendChild(levelBadge);
            factCard.appendChild(factP);
            factCard.classList.add('pixel-assembling');
            container.appendChild(factCard);
        } else {
            
            factP.textContent = factText;
            factCard.appendChild(levelBadge);
            factCard.appendChild(factP);
            if (!isFirstHint) {
                factCard.classList.add('fact-slide-in');
            }
            container.appendChild(factCard);
        }
        
        
        document.getElementById('guessLevel').textContent = currentHintLevel;
    }
}


function animateDecryption(element, targetText) {
    const chars = 'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ0123456789@#$%&*';
    const duration = 1500;
    const frameRate = 30;
    const totalFrames = duration / (1000 / frameRate);
    let frame = 0;
    
    element.classList.add('decrypting');
    
    const interval = setInterval(() => {
        frame++;
        const progress = frame / totalFrames;
        
        let result = '';
        for (let i = 0; i < targetText.length; i++) {
            if (targetText[i] === ' ') {
                result += ' ';
            } else if (i < targetText.length * progress) {
                result += targetText[i];
            } else {
                result += chars[Math.floor(Math.random() * chars.length)];
            }
        }
        
        element.textContent = result;
        
        if (frame >= totalFrames) {
            clearInterval(interval);
            element.textContent = targetText;
            element.classList.remove('decrypting');
        }
    }, 1000 / frameRate);
}

function checkGuess() {
    const input = document.getElementById('guessInput');
    const userAnswer = input?.value.trim();
    
    if (!userAnswer) {
        showToast(t('enterAnswer') || 'Жауапты енгізіңіз', 'warning');
        return;
    }
    
    if (!currentFigure) return;
    
    
    const correctNames = Object.values(currentFigure.name).filter(Boolean);
    const isCorrect = correctNames.some(name => isGuessCloseEnough(userAnswer, name));
    
    if (isCorrect) {
        
        showGuessResult(true);
    } else {
        
        if (currentHintLevel < 3) {
            
            currentHintLevel++;
            showCurrentHint();
            showToast(t('wrongTryAgain') || 'Қате! Келесі факт көрсетілді', 'warning');
            input.value = '';
            input.focus();
        } else {
            
            showGuessResult(false);
        }
    }
}

function skipGuess() {
    showGuessResult(false);
}

function showGuessResult(isCorrect) {
    document.getElementById('guessPlayScreen')?.classList.add('hidden');
    document.getElementById('guessResultScreen')?.classList.remove('hidden');
    
    const correctName = currentFigure.name[currentLang] || currentFigure.name.ru;
    
    if (isCorrect) {
        guessStreak++;
        if (guessStreak > guessBestStreak) {
            guessBestStreak = guessStreak;
        }
        
        userStats.guessStreak = guessStreak;
        userStats.guessBestStreak = guessBestStreak;
        saveUserStats();
        
        document.getElementById('guessResultIcon').textContent = '🎉';
        document.getElementById('guessResultTitle').textContent = t('correct') || 'Дұрыс!';
    } else {
        guessStreak = 0;
        userStats.guessStreak = 0;
        saveUserStats();
        
        document.getElementById('guessResultIcon').textContent = '😔';
        document.getElementById('guessResultTitle').textContent = t('incorrect') || 'Қате!';
    }
    
    document.getElementById('guessResultAnswer').textContent = correctName;
    
    const infoEl = document.getElementById('guessResultInfo');
    if (infoEl) {
        infoEl.innerHTML = '';
        const ul = document.createElement('ul');
        ul.style.margin = '0';
        ul.style.paddingLeft = '18px';
        ul.style.textAlign = 'left';
        shownFactsInRound.forEach((f) => {
            const li = document.createElement('li');
            const txt = f?.text?.[currentLang] || f?.text?.ru || f?.text?.en || '';
            li.textContent = txt;
            ul.appendChild(li);
        });
        infoEl.appendChild(ul);
    }
    
    
    document.getElementById('guessStreak').textContent = guessStreak;
    document.getElementById('guessBestStreak').textContent = guessBestStreak;
}

async function nextGuessRound() {
    const data = await loadGuessGameData();
    if (!data) return;
    
    pickNewFigure(data);
    
    document.getElementById('guessResultScreen')?.classList.add('hidden');
    document.getElementById('guessPlayScreen')?.classList.remove('hidden');
}

function endGuessGame() {
    document.getElementById('guessResultScreen')?.classList.add('hidden');
    document.getElementById('guessStartScreen')?.classList.remove('hidden');
    
    
    document.getElementById('guessStreak').textContent = guessStreak;
    document.getElementById('guessBestStreak').textContent = guessBestStreak;
}

// ==================== QUICK ACTION BUTTONS ====================
function showLearnMode() {
    
    openAITeacherModal('learn');
}

function showPracticeMode() {
    
    openAITeacherModal('practice');
}

function showRealTestMode() {
    
    openAITeacherModal('realtest');
}

// ==================== TEST EDITOR FUNCTIONS ====================
let testEditorQuestions = [];
let testQuestionCounter = 0;

// ==================== TEST EDITOR PAGE FUNCTIONS ====================
let editorQuestions = [];
let currentQuestionIndex = 0;
let editorVisibility = 'public';
let editorQuestionOrder = 'static'; 
let editorAnswerRevealMode = 'immediate';
let editorHintsEnabled = true;
let pendingQuestionImageIndex = null;
let editorDraftImagePaths = new Set();
let editingTarget = null; 

function showTestEditorPage() {
    closeAllSidePanels();
    closeAllModals();
    document.getElementById('blurOverlay')?.classList.remove('active');
    hideAllPages();
    document.getElementById('testEditorPage')?.classList.remove('hidden');
    
    
    cleanupDraftQuestionImages().catch((error) => {
        console.warn('Draft image cleanup error:', error);
    });

    editorQuestions = [{
        text: '',
        image: null,
        imagePath: null,
        options: ['', ''],
        correctIndex: 0
    }];
    currentQuestionIndex = 0;
    editorVisibility = 'public';
    editorQuestionOrder = 'static';
    editorAnswerRevealMode = 'immediate';
    editorHintsEnabled = true;
    pendingQuestionImageIndex = null;
    editorDraftImagePaths = new Set();
    
    
    document.getElementById('editorTestName').value = '';
    document.getElementById('editorTestSubject').value = 'history_kz';
    
    
    document.querySelectorAll('.visibility-btn[data-value]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.value === 'public');
    });

    
    document.querySelectorAll('.visibility-btn[data-order]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.order === 'static');
    });

    document.querySelectorAll('.visibility-btn[data-answer-mode]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.answerMode === 'immediate');
    });

    document.querySelectorAll('.visibility-btn[data-hints]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.hints === 'yes');
    });

    if (AITeacher && !AITeacher._inFlight) {
        hideAILoading();
    }
    
    
    goToEditorStep(1);
}

function cancelTestEditor() {
    cleanupDraftQuestionImages().catch((error) => {
        console.warn('Draft image cleanup error:', error);
    });
    showLibrary();
}

function confirmExitEditor() {
    
    const hasContent = editorQuestions.some(q => q.text || q.image || q.options.some(o => o)) ||
                      document.getElementById('editorTestName')?.value?.trim();
    
    if (hasContent) {
        
        if (confirm(t('exitEditorWarning'))) {
             forceExitEditor();
        }
    } else {
        showHome(true); 
    }
}

function forceExitEditor() {
    closeModal('exitEditorModal');
    cleanupDraftQuestionImages().catch((error) => {
        console.warn('Draft image cleanup error:', error);
    });
    showHome(true); 
}

function setTestVisibility(value) {
    editorVisibility = value;
    document.querySelectorAll('.visibility-btn[data-value]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.value === value);
    });
}

function setQuestionOrder(value) {
    editorQuestionOrder = value === 'random' ? 'random' : 'static';
    document.querySelectorAll('.visibility-btn[data-order]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.order === editorQuestionOrder);
    });
}

function setAnswerRevealMode(value) {
    editorAnswerRevealMode = normalizeAnswerRevealMode(value);
    document.querySelectorAll('.visibility-btn[data-answer-mode]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.answerMode === editorAnswerRevealMode);
    });
}

function setHintsEnabled(value) {
    editorHintsEnabled = !!value;
    document.querySelectorAll('.visibility-btn[data-hints]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.hints === (editorHintsEnabled ? 'yes' : 'no'));
    });
}

function normalizeAnswerRevealMode(value) {
    return value === 'end' ? 'end' : 'immediate';
}

function normalizeHintsEnabled(value, fallback = false) {
    if (typeof value === 'boolean') return value;
    if (typeof value === 'string') {
        const normalized = value.trim().toLowerCase();
        if (['1', 'true', 'yes', 'on'].includes(normalized)) return true;
        if (['0', 'false', 'no', 'off'].includes(normalized)) return false;
    }
    return fallback;
}

function normalizeQuestionsPayload(payload) {
    const normalizeQuestionItem = (item) => {
        if (!item || typeof item !== 'object') return item;
        const image = item.image || item.imageUrl || item.image_url || null;
        const imagePath = item.imagePath || item.image_path || null;
        return { ...item, image, imagePath };
    };

    if (!payload) {
        return { items: [], order: 'static', answerMode: 'immediate', hintsEnabled: false };
    }
    if (Array.isArray(payload)) {
        return { items: payload.map(normalizeQuestionItem), order: 'static', answerMode: 'immediate', hintsEnabled: false };
    }
    if (typeof payload === 'object' && Array.isArray(payload.items)) {
        return {
            items: payload.items.map(normalizeQuestionItem),
            order: payload.order === 'random' ? 'random' : 'static',
            answerMode: normalizeAnswerRevealMode(payload.answerMode || payload.answer_mode),
            hintsEnabled: normalizeHintsEnabled(payload.hintsEnabled ?? payload.hints_enabled, false)
        };
    }
    return { items: [], order: 'static', answerMode: 'immediate', hintsEnabled: false };
}

function goToEditorStep(step) {
    if (step === 2) {
        const testName = document.getElementById('editorTestName')?.value?.trim();
        if (!testName) {
            showToast(t('enterTestName'), 'warning');
            document.getElementById('editorTestName')?.focus();
            return;
        }
    }
    document.querySelectorAll('.test-editor-step').forEach(s => s.classList.add('hidden'));
    document.getElementById(`testEditorStep${step}`)?.classList.remove('hidden');
    
    if (step === 2) {
        renderCurrentQuestion();
        updateQuestionNavigation();
    }
}

function renderCurrentQuestion() {
    const question = editorQuestions[currentQuestionIndex];
    if (!question) return;
    
    
    const questionBtn = document.getElementById('questionTextBtn');
    if (questionBtn) {
        const placeholder = questionBtn.querySelector('.question-placeholder');
        if (question.text) {
            placeholder.textContent = question.text;
            questionBtn.classList.add('has-content');
        } else {
            placeholder.textContent = t('clickToAddQuestion');
            questionBtn.classList.remove('has-content');
        }
    }
    
    const imageArea = document.getElementById('questionImageArea');
    if (imageArea) {
        imageArea.innerHTML = '';
        if (question.image) {
            const img = document.createElement('img');
            img.className = 'question-image';
            img.src = question.image;
            img.alt = t('questionImageAlt') || 'Question image';
            imageArea.appendChild(img);
            imageArea.classList.add('has-image');
        } else {
            const placeholder = document.createElement('div');
            placeholder.className = 'image-placeholder';

            const icon = document.createElement('span');
            icon.className = 'image-icon';
            icon.textContent = '🖼️';

            const text = document.createElement('span');
            text.textContent = t('addImage');

            placeholder.appendChild(icon);
            placeholder.appendChild(text);
            imageArea.appendChild(placeholder);
            imageArea.classList.remove('has-image');
        }
    }

    const optionsContainer = document.getElementById('answerOptions');
    if (optionsContainer) {
        optionsContainer.innerHTML = '';
        
        const letters = ['A', 'B', 'C', 'D'];
        question.options.forEach((opt, i) => {
            const btn = document.createElement('button');
            btn.className = `answer-option-btn ${opt ? 'has-content' : ''} ${i === question.correctIndex ? 'correct' : ''}`;
            btn.dataset.index = i;
            btn.onclick = () => editAnswerOption(i);

            const letterSpan = document.createElement('span');
            letterSpan.className = 'option-letter';
            letterSpan.textContent = letters[i];

            const textSpan = document.createElement('span');
            textSpan.className = 'option-text';
            
            textSpan.textContent = opt || t('clickToAddAnswer');

            const correctSpan = document.createElement('span');
            correctSpan.className = `correct-indicator ${i === question.correctIndex ? '' : 'hidden'}`;
            correctSpan.textContent = '✓';

            btn.appendChild(letterSpan);
            btn.appendChild(textSpan);
            btn.appendChild(correctSpan);
            
            optionsContainer.appendChild(btn);
        });

    }
    
    
    const addOptionBtn = document.getElementById('addOptionBtn');
    if (addOptionBtn) {
        addOptionBtn.style.display = question.options.length < 4 ? 'flex' : 'none';
    }
    
    
    const deleteBtn = document.getElementById('deleteQuestionBtn');
    if (deleteBtn) {
        deleteBtn.style.display = editorQuestions.length > 1 ? 'flex' : 'none';
    }
}

function deleteCurrentQuestion() {
    if (editorQuestions.length <= 1) return;
    
    const [removedQuestion] = editorQuestions.splice(currentQuestionIndex, 1);
    const removedImagePath = removedQuestion?.imagePath || extractStorageObjectPath(removedQuestion?.image);
    if (removedImagePath) {
        editorDraftImagePaths.delete(removedImagePath);
        removeQuestionImageFromStorage(removedImagePath).catch((error) => {
            console.warn('Question image delete error:', error);
        });
    }
    
    
    if (currentQuestionIndex >= editorQuestions.length) {
        currentQuestionIndex = editorQuestions.length - 1;
    }
    
    renderCurrentQuestion();
    updateQuestionNavigation();
}

function updateQuestionNavigation() {
    const navScroll = document.getElementById('questionNavScroll');
    if (!navScroll) return;
    
    navScroll.innerHTML = '';
    
    editorQuestions.forEach((q, i) => {
        const btn = document.createElement('button');
        btn.className = `question-nav-btn ${i === currentQuestionIndex ? 'active' : ''} ${q.text && q.options.some(o => o) ? 'completed' : ''}`;
        btn.dataset.index = i;
        btn.textContent = i + 1;
        btn.onclick = () => switchToQuestion(i);
        navScroll.appendChild(btn);
    });
}

function switchToQuestion(index) {
    if (index < 0 || index >= editorQuestions.length) return;
    currentQuestionIndex = index;
    renderCurrentQuestion();
    updateQuestionNavigation();
}

function addNewQuestion() {
    editorQuestions.push({
        text: '',
        image: null,
        imagePath: null,
        options: ['', ''],
        correctIndex: 0
    });
    switchToQuestion(editorQuestions.length - 1);
}

function addAnswerOption() {
    const question = editorQuestions[currentQuestionIndex];
    if (!question || question.options.length >= 4) return;
    
    question.options.push('');
    renderCurrentQuestion();
}

function editQuestionText() {
    editingTarget = 'question';
    const question = editorQuestions[currentQuestionIndex];
    openEditOverlay(question?.text || '', false);
}

function editAnswerOption(index) {
    editingTarget = `option-${index}`;
    const question = editorQuestions[currentQuestionIndex];
    openEditOverlay(question?.options[index] || '', true, index);
}

function openEditOverlay(initialText, isOption = false, optionIndex = -1) {
    const overlay = document.getElementById('editOverlay');
    const textarea = document.getElementById('editTextarea');
    const markCorrectBtn = document.getElementById('markCorrectBtn');
    const deleteOptionBtn = document.getElementById('deleteOptionBtn');
    
    if (overlay && textarea) {
        textarea.value = initialText;
        overlay.classList.remove('hidden');
        textarea.focus();

        
        
        if (markCorrectBtn) {
            if (isOption && optionIndex >= 0) {
                markCorrectBtn.classList.remove('hidden');
                markCorrectBtn.dataset.optionIndex = optionIndex;
            } else {
                markCorrectBtn.classList.add('hidden');
            }
        }
        
        
        if (deleteOptionBtn) {
            const question = editorQuestions[currentQuestionIndex];
            const isCorrectAnswer = question && question.correctIndex === optionIndex;
            const hasMultipleOptions = question && question.options.filter(o => o).length > 2;
            
            if (isOption && optionIndex >= 0 && !isCorrectAnswer && hasMultipleOptions) {
                deleteOptionBtn.classList.remove('hidden');
                deleteOptionBtn.dataset.optionIndex = optionIndex;
                deleteOptionBtn.disabled = false;
            } else {
                deleteOptionBtn.classList.add('hidden');
            }
        }
    }
}

function markAsCorrectAnswer() {
    const markCorrectBtn = document.getElementById('markCorrectBtn');
    const optionIndex = parseInt(markCorrectBtn?.dataset.optionIndex || '0');
    const question = editorQuestions[currentQuestionIndex];
    
    if (question && optionIndex >= 0) {
        question.correctIndex = optionIndex;
        showToast(t('markedAsCorrect'), 'success');
    }
    
    
    saveEditedText();
}

function closeEditOverlay() {
    document.getElementById('editOverlay')?.classList.add('hidden');
    document.getElementById('markCorrectBtn')?.classList.add('hidden');
    document.getElementById('deleteOptionBtn')?.classList.add('hidden');
    const textarea = document.getElementById('editTextarea');
    if (textarea) textarea.oninput = null;
    editingTarget = null;
}

function deleteCurrentOption() {
    const deleteOptionBtn = document.getElementById('deleteOptionBtn');
    const optionIndex = parseInt(deleteOptionBtn?.dataset.optionIndex || '-1');
    const question = editorQuestions[currentQuestionIndex];
    
    if (!question || optionIndex < 0 || optionIndex >= question.options.length) {
        closeEditOverlay();
        return;
    }
    
    
    if (question.correctIndex === optionIndex) {
        showToast(t('cantDeleteCorrectAnswer'), 'warning');
        return;
    }
    
    
    const filledOptions = question.options.filter(o => o).length;
    if (filledOptions <= 2) {
        showToast(t('minTwoOptions'), 'warning');
        return;
    }
    
    
    question.options.splice(optionIndex, 1);
    
    
    if (question.correctIndex > optionIndex) {
        question.correctIndex--;
    }
    
    closeEditOverlay();
    renderCurrentQuestion();
    showToast(t('optionDeleted'), 'success');
}

function saveEditedText() {
    const textarea = document.getElementById('editTextarea');
    const text = textarea?.value?.trim() || '';
    const question = editorQuestions[currentQuestionIndex];
    
    if (!question || !editingTarget) {
        closeEditOverlay();
        return;
    }
    
    if (editingTarget === 'question') {
        question.text = text;
    } else if (editingTarget.startsWith('option-')) {
        const index = parseInt(editingTarget.split('-')[1]);
        question.options[index] = text;
    }
    
    closeEditOverlay();
    renderCurrentQuestion();
    updateQuestionNavigation();
}

function getQuestionImageFileExtension(file) {
    const fromName = String(file?.name || '').split('.').pop()?.toLowerCase() || '';
    if (fromName && /^[a-z0-9]{2,5}$/i.test(fromName)) {
        if (fromName === 'jpeg') return 'jpg';
        return fromName;
    }
    const mime = String(file?.type || '').toLowerCase();
    const map = {
        'image/jpeg': 'jpg',
        'image/png': 'png',
        'image/webp': 'webp',
        'image/gif': 'gif',
        'image/avif': 'avif',
        'image/svg+xml': 'svg'
    };
    return map[mime] || 'bin';
}

function extractStorageObjectPath(pathOrUrl, bucketId = TEST_IMAGES_BUCKET) {
    if (!pathOrUrl || typeof pathOrUrl !== 'string') return null;
    const value = pathOrUrl.trim();
    if (!value) return null;

    if (!/^https?:\/\//i.test(value)) {
        return value.replace(/^\/+/, '');
    }

    try {
        const url = new URL(value);
        const candidates = [
            `/storage/v1/object/public/${bucketId}/`,
            `/storage/v1/object/authenticated/${bucketId}/`,
            `/storage/v1/object/sign/${bucketId}/`
        ];
        for (const marker of candidates) {
            const index = url.pathname.indexOf(marker);
            if (index !== -1) {
                const rawPath = url.pathname.slice(index + marker.length);
                return decodeURIComponent(rawPath);
            }
        }
    } catch {
        return null;
    }

    return null;
}

async function removeQuestionImageFromStorage(pathOrUrl) {
    const objectPath = extractStorageObjectPath(pathOrUrl);
    if (!objectPath) return;

    const ready = await ensureSupabaseReady();
    if (!ready || !supabaseClient) return;

    try {
        const { error } = await supabaseClient
            .storage
            .from(TEST_IMAGES_BUCKET)
            .remove([objectPath]);
        if (error) {
            console.warn('Storage remove error:', error);
        }
    } catch (error) {
        console.warn('Storage remove error:', error);
    }
}

function consumeDraftImagePaths() {
    const paths = Array.from(editorDraftImagePaths || []).filter(Boolean);
    editorDraftImagePaths = new Set();
    return paths;
}

async function cleanupDraftQuestionImages() {
    const paths = consumeDraftImagePaths();
    if (paths.length === 0) return;

    const ready = await ensureSupabaseReady();
    if (!ready || !supabaseClient) return;

    try {
        const { error } = await supabaseClient
            .storage
            .from(TEST_IMAGES_BUCKET)
            .remove(paths);
        if (error) {
            console.warn('Draft image cleanup error:', error);
        }
    } catch (error) {
        console.warn('Draft image cleanup error:', error);
    }
}

async function uploadQuestionImageFile(file, questionIndex) {
    if (!file || !file.type || !file.type.startsWith('image/')) {
        throw new Error('invalid_type');
    }
    if (file.size > TEST_IMAGE_MAX_FILE_SIZE_BYTES) {
        throw new Error('file_too_large');
    }

    if (!currentUser) {
        await ensureSessionLoaded();
    }
    if (!currentUser?.id) {
        throw new Error('auth_required');
    }

    const ready = await ensureSupabaseReady();
    if (!ready || !supabaseClient) {
        throw new Error('supabase_unavailable');
    }

    const extension = getQuestionImageFileExtension(file);
    const randomPart = (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function')
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    const safeQuestionIndex = Number.isInteger(questionIndex) ? questionIndex : 0;
    const objectPath = `${currentUser.id}/tests/${Date.now()}-${randomPart}-q${safeQuestionIndex + 1}.${extension}`;

    const { error: uploadError } = await supabaseClient
        .storage
        .from(TEST_IMAGES_BUCKET)
        .upload(objectPath, file, {
            cacheControl: '31536000',
            upsert: false,
            contentType: file.type || 'application/octet-stream'
        });

    if (uploadError) {
        throw uploadError;
    }

    const { data } = supabaseClient
        .storage
        .from(TEST_IMAGES_BUCKET)
        .getPublicUrl(objectPath);

    const publicUrl = data?.publicUrl;
    if (!publicUrl) {
        await removeQuestionImageFromStorage(objectPath);
        throw new Error('public_url_missing');
    }

    return { publicUrl, objectPath };
}

function addQuestionImage() {
    pendingQuestionImageIndex = currentQuestionIndex;
    const input = document.getElementById('questionImageInput');
    if (!input) return;
    input.value = '';
    input.click();
}

async function handleQuestionImageUpload(event) {
    const input = event?.target;
    const file = input?.files?.[0];
    const targetIndex = Number.isInteger(pendingQuestionImageIndex) ? pendingQuestionImageIndex : currentQuestionIndex;
    pendingQuestionImageIndex = null;

    if (!file) return;
    if (!file.type || !file.type.startsWith('image/')) {
        showToast(t('invalidImageFile') || 'Please select an image file', 'warning');
        if (input) input.value = '';
        return;
    }
    if (file.size > TEST_IMAGE_MAX_FILE_SIZE_BYTES) {
        showToast(t('imageTooLarge') || 'Image is too large (max 5MB)', 'warning');
        if (input) input.value = '';
        return;
    }

    showToast(t('imageUploading') || 'Uploading image...', 'info', 1800);

    try {
        const { publicUrl, objectPath } = await uploadQuestionImageFile(file, targetIndex);
        const question = editorQuestions[targetIndex];
        if (!question) {
            await removeQuestionImageFromStorage(objectPath);
            if (input) input.value = '';
            return;
        }

        const oldPath = question.imagePath || extractStorageObjectPath(question.image);
        question.image = publicUrl;
        question.imagePath = objectPath;
        editorDraftImagePaths.add(objectPath);

        if (oldPath && oldPath !== objectPath) {
            editorDraftImagePaths.delete(oldPath);
            await removeQuestionImageFromStorage(oldPath);
        }

        if (targetIndex === currentQuestionIndex) {
            renderCurrentQuestion();
        }
        updateQuestionNavigation();
        showToast(t('imageUploaded') || 'Image uploaded successfully', 'success');
    } catch (error) {
        console.warn('Question image upload error:', error);
        const message = String(error?.message || '').toLowerCase();
        if (message.includes('auth_required')) {
            showToast(t('pleaseLogin') || 'Please login', 'warning');
        } else if (message.includes('file_too_large')) {
            showToast(t('imageTooLarge') || 'Image is too large (max 5MB)', 'warning');
        } else {
            showToast(t('imageUploadError') || 'Failed to upload image', 'error');
        }
    }

    if (input) input.value = '';
}

async function saveTestFromEditor() {
    if (isTestSaving) return;
    isTestSaving = true;
    const testName = document.getElementById('editorTestName')?.value?.trim();
    const subject = document.getElementById('editorTestSubject')?.value;
    const isPublic = editorVisibility === 'public';

    if (!currentUser) {
        await ensureSessionLoaded();
    }
    
    if (!testName) {
        showToast(t('enterTestName'), 'warning');
        goToEditorStep(1);
        isTestSaving = false;
        return;
    }
    
    
    const validQuestions = editorQuestions.filter(q => 
        q.text && q.options.filter(o => o).length >= 2
    );
    
    if (validQuestions.length === 0) {
        showToast(t('addAtLeastOneQuestion'), 'warning');
        isTestSaving = false;
        return;
    }
    
    
    const questions = validQuestions.map(q => {
        const options = (q.options || []).filter(o => o);
        const chosen = q.options?.[q.correctIndex];
        const correctAnswerText = chosen && chosen.trim() ? chosen : (options[0] || '');
        const mappedCorrectIndex = Math.max(options.indexOf(correctAnswerText), 0);
        const imageUrl = q.image || q.imageUrl || q.image_url || null;
        const imagePath = q.imagePath || q.image_path || null;
        return {
            question: q.text,
            image: imageUrl,
            imagePath: imagePath,
            options,
            correctIndex: mappedCorrectIndex
        };
    });
    const questionsPayload = {
        order: editorQuestionOrder,
        answerMode: editorAnswerRevealMode,
        hintsEnabled: editorHintsEnabled,
        items: questions
    };
    
    const content = questions.map((q, i) => {
        return `${i + 1}. ${q.question}: ${q.options[q.correctIndex] || q.options[0]}`;
    }).join('\n');
    
    const testData = {
        id: Date.now(),
        title: testName,
        subject: subject,
        type: 'test',
        is_public: isPublic,
        content: content,
        questions: questions,
        question_order: editorQuestionOrder,
        answer_mode: editorAnswerRevealMode,
        hints_enabled: editorHintsEnabled,
        author: currentUser?.user_metadata?.username || userProfile?.username || t('guest'),
        author_id: currentUser?.id || null,
        created_at: new Date().toISOString(),
        count: questions.length
    };
    
    
    if (currentUser) {
        const ready = await ensureSupabaseReady();
        if (!ready) {
            showToast(t('errorSavingTest') || 'Error saving test', 'error');
            isTestSaving = false;
            return;
        }
        try {
            const insertPayloadBase = {
                user_id: currentUser.id,
                title: testName,
                subject: subject,
                is_public: isPublic,
                content: content,
                author: testData.author,
                count: questions.length
            };

            
            
            
            let data = null;
            let error = null;

            ({ data, error } = await supabaseClient
                .from('tests')
                .insert([{ ...insertPayloadBase, questions: questionsPayload }])
                .select()
                .single());

            if (error) {
                console.warn('Insert with JSON questions failed, retrying as string...', error);
                ({ data, error } = await supabaseClient
                    .from('tests')
                    .insert([{ ...insertPayloadBase, questions: JSON.stringify(questionsPayload) }])
                    .select()
                    .single());
            }

            if (error) {
                console.error('Error saving test:', error);
                showToast(`${t('errorSavingTest')}: ${error.message || error}`, 'error');
                isTestSaving = false;
                return;
            }

            testData.id = data.id;
            userTests.push(testData);
            console.log('Test saved to Supabase:', data);
        } catch (err) {
            console.error('Error saving test:', err);
            showToast(`${t('errorSavingTest')}: ${err.message || err}`, 'error');
            isTestSaving = false;
            return;
        }
    } else {
        showToast(t('pleaseLogin'), 'warning');
        isTestSaving = false;
        return;
    }
    
    showToast(t('testSaved'), 'success');
    editorDraftImagePaths.clear();
    supabaseTestsLoaded = false; 
    showLibrary();
    isTestSaving = false;
}

function openTestEditorModal() {
    testEditorQuestions = [];
    testQuestionCounter = 0;
    
    
    const questionsList = document.getElementById('testQuestionsList');
    if (questionsList) {
        questionsList.innerHTML = '';
    }
    
    
    const nameInput = document.getElementById('testEditorName');
    const subjectSelect = document.getElementById('testEditorSubject');
    const publicRadio = document.querySelector('input[name="testVisibility"][value="public"]');
    
    if (nameInput) nameInput.value = '';
    if (subjectSelect) subjectSelect.selectedIndex = 0;
    if (publicRadio) publicRadio.checked = true;
    
    
    addNewTestQuestion();
    
    openModal('testEditorModal');
}

function addNewTestQuestion() {
    testQuestionCounter++;
    const questionId = testQuestionCounter;
    
    const questionsList = document.getElementById('testQuestionsList');
    if (!questionsList) return;
    
    const questionCard = document.createElement('div');
    questionCard.className = 'test-question-card';
    questionCard.id = `question-card-${questionId}`;
    
    questionCard.innerHTML = `
        <div class="test-question-header">
            <h4>${t('quizQuestion')} ${questionId}</h4>
            <button type="button" class="test-question-delete" onclick="deleteTestQuestion(${questionId})" title="${t('delete')}">✕</button>
        </div>
        <div class="test-question-input">
            <input type="text" class="form-input" id="question-text-${questionId}" placeholder="${t('enterQuestion')}" required>
        </div>
        <div class="test-options-list">
            <div class="test-option-row">
                <span class="option-label">A)</span>
                <input type="radio" name="correct-${questionId}" value="0" checked>
                <input type="text" class="form-input" id="option-${questionId}-0" placeholder="${t('option')} A">
            </div>
            <div class="test-option-row">
                <span class="option-label">B)</span>
                <input type="radio" name="correct-${questionId}" value="1">
                <input type="text" class="form-input" id="option-${questionId}-1" placeholder="${t('option')} B">
            </div>
            <div class="test-option-row">
                <span class="option-label">C)</span>
                <input type="radio" name="correct-${questionId}" value="2">
                <input type="text" class="form-input" id="option-${questionId}-2" placeholder="${t('option')} C">
            </div>
            <div class="test-option-row">
                <span class="option-label">D)</span>
                <input type="radio" name="correct-${questionId}" value="3">
                <input type="text" class="form-input" id="option-${questionId}-3" placeholder="${t('option')} D">
            </div>
        </div>
    `;
    
    questionsList.appendChild(questionCard);
    
    
    testEditorQuestions.push({
        id: questionId,
        text: '',
        options: ['', '', '', ''],
        correctIndex: 0
    });
}

function deleteTestQuestion(questionId) {
    const questionCard = document.getElementById(`question-card-${questionId}`);
    if (questionCard) {
        questionCard.remove();
    }
    
    
    testEditorQuestions = testEditorQuestions.filter(q => q.id !== questionId);
    
    
    if (testEditorQuestions.length === 0) {
        addNewTestQuestion();
    }
}

function collectTestData() {
    const questions = [];
    
    testEditorQuestions.forEach(q => {
        const questionText = document.getElementById(`question-text-${q.id}`)?.value?.trim();
        const options = [];
        let correctIndex = 0;
        
        for (let i = 0; i < 4; i++) {
            const optionText = document.getElementById(`option-${q.id}-${i}`)?.value?.trim();
            options.push(optionText || '');
        }
        
        const correctRadio = document.querySelector(`input[name="correct-${q.id}"]:checked`);
        if (correctRadio) {
            correctIndex = parseInt(correctRadio.value);
        }
        
        if (questionText && options.some(opt => opt)) {
            questions.push({
                question: questionText,
                options: options.filter(opt => opt),
                correctIndex: correctIndex
            });
        }
    });
    
    return questions;
}

async function saveNewTest() {
    if (isTestSaving) return;
    isTestSaving = true;
    const testName = document.getElementById('testEditorName')?.value?.trim();
    const subject = document.getElementById('testEditorSubject')?.value;
    const visibilityRadio = document.querySelector('input[name="testVisibility"]:checked');
    const isPublic = visibilityRadio?.value === 'public';

    if (!currentUser) {
        await ensureSessionLoaded();
    }
    
    if (!testName) {
        showToast(t('enterTestName'), 'warning');
        isTestSaving = false;
        return;
    }
    
    const questions = collectTestData();
    
    if (questions.length === 0) {
        showToast(t('addAtLeastOneQuestion'), 'warning');
        isTestSaving = false;
        return;
    }
    
    
    const content = questions.map((q, i) => {
        return `${i + 1}. ${q.question}: ${q.options[q.correctIndex]}`;
    }).join('\n');
    
    
    const testData = {
        id: Date.now(),
        title: testName,
        subject: subject,
        type: 'test',
        is_public: isPublic,
        content: content,
        questions: questions,
        author: currentUser?.user_metadata?.username || userProfile?.username || t('guest'),
        author_id: currentUser?.id || null,
        created_at: new Date().toISOString(),
        count: questions.length
    };

    const questionsPayload = {
        order: 'static',
        items: questions
    };
    
    
    if (currentUser) {
        const ready = await ensureSupabaseReady();
        if (!ready) {
            showToast(t('errorSavingTest') || 'Error saving test', 'error');
            isTestSaving = false;
            return;
        }
        try {
            const insertPayloadBase = {
                user_id: currentUser.id,
                title: testName,
                subject: subject,
                is_public: isPublic,
                content: content,
                author: testData.author,
                count: questions.length
            };

            let data = null;
            let error = null;

            ({ data, error } = await supabaseClient
                .from('tests')
                .insert([{ ...insertPayloadBase, questions: questionsPayload }])
                .select()
                .single());

            if (error) {
                console.warn('Insert with JSON questions failed, retrying as string...', error);
                ({ data, error } = await supabaseClient
                    .from('tests')
                    .insert([{ ...insertPayloadBase, questions: JSON.stringify(questionsPayload) }])
                    .select()
                    .single());
            }

            if (error) {
                console.error('Error saving test to Supabase:', error);
                showToast(`${t('errorSavingTest')}: ${error.message || error}`, 'error');
                isTestSaving = false;
                return;
            }

            testData.id = data.id;
            testData.supabase_id = data.id;
            userTests.push(testData);
            console.log('Test saved to Supabase:', data);
        } catch (err) {
            console.error('Error saving test:', err);
            showToast(`${t('errorSavingTest')}: ${err.message || err}`, 'error');
            isTestSaving = false;
            return;
        }
    } else {
        showToast(t('pleaseLogin'), 'warning');
        isTestSaving = false;
        return;
    }
    
    showToast(t('testSaved'), 'success');
    closeModal('testEditorModal');
    
    
    showLibrary();
    isTestSaving = false;
}

// ==================== IMPROVED CLASSMATES FUNCTION ====================
async function renderClassmates() {
    const list = document.getElementById('classmatesList');
    const emptyState = document.getElementById('emptyClassmates');
    const countEl = document.getElementById('classmatesCount');
    const schoolEl = document.getElementById('classmatesSchool');
    const classNameEl = document.getElementById('classmatesClassName');
    
    if (!list) return;
    
    const profile = userProfile || {};
    const userMeta = currentUser?.user_metadata || {};
    
    
    const userCity = profile.city || userMeta.city || '';
    const userSchool = profile.school || userMeta.school || '';
    const userClass = profile.class || userMeta.class || '';
    
    const schoolNames = {
        dostyq: 'Dostyq School',
        nis: 'NIS',
        bil: 'БИЛ',
        other: 'Мектеп'
    };
    
    
    if (schoolEl) schoolEl.textContent = schoolNames[userSchool] || userSchool;
    if (classNameEl) classNameEl.textContent = userClass;
    
    
    list.innerHTML = `<div class="loading-state">${t('loading')}...</div>`;
    
    let classmates = [];

    try {
        if (supabaseClient && userCity && userSchool && userClass) {
            const supabaseUrl = supabaseClient.supabaseUrl;
            const supabaseKey = supabaseClient.supabaseKey;
            const url = `${supabaseUrl}/rest/v1/profiles?select=*&city=eq.${encodeURIComponent(userCity)}&school=eq.${encodeURIComponent(userSchool)}&class=eq.${encodeURIComponent(userClass)}`;
            const cacheKey = `classmates:${userCity}:${userSchool}:${userClass}`;

            const profiles = await fetchJsonWithControl(url, {
                headers: {
                    'apikey': supabaseKey,
                    'Authorization': `Bearer ${supabaseKey}`,
                    'Content-Type': 'application/json'
                },
                timeoutMs: 5000,
                cacheKey,
                cacheTtlMs: 15 * 1000
            });

            classmates = (profiles || []).map(p => ({
                id: p.user_id,
                username: p.username || 'Пайдаланушы',
                avatar: p.avatar_url,
                isCurrentUser: p.user_id === currentUser?.id,
                subject1: p.subject1,
                subject2: p.subject2,
                isOnline: onlineUsers.some(u => u.id === p.user_id)
            }));
        }
    } catch (error) {
        console.error('Error fetching classmates:', error);
    }

    
    const username = profile.username || userMeta.username || currentUser?.email?.split('@')[0];
    if (classmates.length === 0 && currentUser && username) {
        classmates.push({ 
            id: currentUser.id,
            username: username, 
            avatar: userAvatar, 
            isCurrentUser: true,
            subject1: profile.subject1 || userMeta.subject1,
            subject2: profile.subject2 || userMeta.subject2,
            isOnline: true
        });
    }
    
    if (countEl) countEl.textContent = classmates.length;
    
    if (classmates.length === 0) {
        list.innerHTML = '';
        emptyState?.classList.remove('hidden');
        return;
    }
    
    emptyState?.classList.add('hidden');
    
    const subjectNames = getSubjectNames();
    
    
    list.innerHTML = '';

    
    classmates.sort((a, b) => {
        if (a.isCurrentUser) return -1;
        if (b.isCurrentUser) return 1;
        if (a.isOnline && !b.isOnline) return -1;
        if (!a.isOnline && b.isOnline) return 1;
        return a.username.localeCompare(b.username);
    });

    classmates.forEach(classmate => {
        const classmateItem = document.createElement('div');
        classmateItem.className = `classmate-item ${classmate.isCurrentUser ? 'current-user' : ''}`;

        const avatarDiv = document.createElement('div');
        avatarDiv.className = 'classmate-avatar';

        if (classmate.avatar) {
            const img = document.createElement('img');
            img.src = classmate.avatar;
            img.alt = '';
            avatarDiv.appendChild(img);
        } else {
            const span = document.createElement('span');
            span.textContent = classmate.username ? classmate.username.charAt(0).toUpperCase() : '?';
            avatarDiv.appendChild(span);
        }

        const infoDiv = document.createElement('div');
        infoDiv.className = 'classmate-info';

        const nameDiv = document.createElement('div');
        nameDiv.className = 'classmate-name';
        nameDiv.textContent = classmate.username || t('guest');

        if (classmate.isCurrentUser) {
            const youBadge = document.createElement('span');
            youBadge.className = 'you-badge';
            youBadge.textContent = `(${t('you')})`;
            nameDiv.appendChild(document.createTextNode(' '));
            nameDiv.appendChild(youBadge);
        }
        
        
        if (classmate.isOnline && !classmate.isCurrentUser) {
            const onlineIndicator = document.createElement('span');
            onlineIndicator.className = 'online-indicator';
            onlineIndicator.innerHTML = '🟢';
            onlineIndicator.title = 'Онлайн';
            nameDiv.appendChild(document.createTextNode(' '));
            nameDiv.appendChild(onlineIndicator);
        }

        const subjectsDiv = document.createElement('div');
        subjectsDiv.className = 'classmate-subjects';
        const subj1 = subjectNames[classmate.subject1] || '';
        const subj2 = subjectNames[classmate.subject2] || '';
        subjectsDiv.textContent = subj1 && subj2 ? `${subj1} • ${subj2}` : (subj1 || subj2 || '');

        infoDiv.appendChild(nameDiv);
        infoDiv.appendChild(subjectsDiv);

        classmateItem.appendChild(avatarDiv);
        classmateItem.appendChild(infoDiv);

        list.appendChild(classmateItem);
    });
}

// ==================== FAVORITES BY SUBJECT FILTER ====================
function filterFavoritesBySubject() {
    const subject = document.getElementById('favoritesSubjectFilter')?.value || 'all';
    renderFavorites(subject);
}

// ==================== TEST TAKING FROM LIBRARY ====================
let testTakingQuestions = [];
let testTakingCurrentIndex = 0;
let testTakingAnswers = {};
let testTakingHintUsed = {};
let testTakingHiddenAnswers = {};
let testTakingSettings = {
    answerMode: 'immediate',
    hintsEnabled: false
};
let testTakingMaterial = null;
let isInTestEditor = false;
let pendingTestData = null; 

function isOwnTest(testData) {
    if (!testData) return false;
    if (testData.isOwn || testData.isUserMaterial) return true;
    if (currentUser?.id && testData.author_id === currentUser.id) return true;
    return false;
}

function isExternalTestAllowed() {
    return true;
}

function canStartExternalTest(testData) {
    return true;
}


function showTestDetailsModal(testData) {
    if (!testData) return;
    
    pendingTestData = testData;
    
    
    const subjectKey = testData.subject || testData.category || 'other';
    const subjectIconKey = normalizeSubjectKey(subjectKey);
    const subjectName = getSubjectDisplayName(subjectKey);
    const categorySvg = getCategoryIcon(subjectIconKey, true);
    document.getElementById('testDetailCategory').innerHTML = `${categorySvg} ${subjectName}`;
    document.getElementById('testDetailTitle').textContent = testData.title || t('untitled');
    document.getElementById('testDetailAuthor').textContent = testData.author || t('anonymous');
    
    
    const date = testData.created_at || testData.createdAt;
    if (date) {
        const dateObj = new Date(date);
        const formatted = dateObj.toLocaleDateString(currentLang === 'kk' ? 'kk-KZ' : 
                         currentLang === 'ru' ? 'ru-RU' : 'en-US', {
            year: 'numeric', month: 'long', day: 'numeric'
        });
        document.getElementById('testDetailDate').textContent = formatted;
    } else {
        document.getElementById('testDetailDate').textContent = '';
    }
    
    
    const questionCount = testData.questions?.length || testData.count || 0;
    document.getElementById('testDetailQuestions').textContent = questionCount;
    
    const avatarEl = document.getElementById('testDetailAvatar');
    const authorName = testData.author || t('anonymous');
    if (testData.author_avatar) {
        avatarEl.innerHTML = `<img src="${testData.author_avatar}" alt="${authorName}">`;
        avatarEl.classList.remove('avatar-letter');
    } else {
        
        const firstLetter = authorName.charAt(0).toUpperCase();
        avatarEl.innerHTML = firstLetter;
        avatarEl.classList.add('avatar-letter');
    }
    
    
    updateTestDetailFavoriteBtn(testData.id);

    
    const startBtn = document.getElementById('testDetailStartBtn');
    if (startBtn) {
        const allowStart = isOwnTest(testData) || isExternalTestAllowed();
        startBtn.disabled = !allowStart;
        startBtn.classList.toggle('disabled', !allowStart);
    }
    
    openModal('testDetailsModal');
}

function updateTestDetailFavoriteBtn(testId) {
    const isFavorite = favorites.includes(testId);
    const btn = document.getElementById('testDetailFavoriteBtn');
    const icon = document.getElementById('testDetailFavIcon');
    if (btn && icon) {
        btn.classList.toggle('active', isFavorite);
        icon.innerHTML = isFavorite 
            ? '<path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/>'
            : '<path d="M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z"/>';
    }
}

function toggleTestDetailFavorite() {
    if (!pendingTestData) return;
    const testId = pendingTestData.id;
    const favId = testId; 
    const index = favorites.indexOf(favId);
    const wasInFavorites = index > -1;
    
    
    if (wasInFavorites) {
        favorites.splice(index, 1);
        removeFromFavoritesSupabase(testId);
    } else {
        favorites.push(favId);
        addToFavoritesSupabase(testId);
    }
    
    const isNowFavorite = !wasInFavorites;
    
    
    updateTestDetailFavoriteBtnUI(testId, isNowFavorite);

    
    document.querySelectorAll(`.favorite-btn[data-test-id="${testId}"]`).forEach(btn => {
        btn.classList.toggle('favorite-active', isNowFavorite);
        btn.innerHTML = isNowFavorite 
            ? '<svg viewBox="0 0 24 24"><path d="M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>' 
            : '<svg viewBox="0 0 24 24"><path d="M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z"/></svg>';
    });
    
    showToast(wasInFavorites ? t('removedFromFavorites') : t('addedToFavorites'), 'success');
    renderFavorites();
}


function startTestFromModal() {
    closeModal('testDetailsModal');
    if (pendingTestData) {
        startTestFromLibrary(pendingTestData);
        pendingTestData = null;
    }
}

function startTestFromLibrary(testData) {
    if (!testData || !testData.questions) {
        showToast(t('errorFormat'), 'error');
        return;
    }

    if (!canStartExternalTest(testData)) {
        return;
    }
    
    testTakingMaterial = testData;
    const normalized = normalizeQuestionsPayload(testData.questions);
    const order = testData.question_order || normalized.order;
    const answerMode = normalizeAnswerRevealMode(testData.answer_mode || normalized.answerMode);
    const hintsEnabled = normalizeHintsEnabled(testData.hints_enabled, normalized.hintsEnabled);
    const sourceItems = normalized.items || [];
    const items = (order === 'random') ? shuffleArray([...sourceItems]) : sourceItems;

    testTakingSettings = {
        answerMode,
        hintsEnabled
    };

    testTakingQuestions = items.map((q, i) => {
        const options = Array.isArray(q.options) ? q.options : [];
        const parsedCorrectIndex = Number.parseInt(q.correctIndex, 10);
        const correctIndex = Number.isInteger(parsedCorrectIndex) &&
            parsedCorrectIndex >= 0 &&
            parsedCorrectIndex < options.length
            ? parsedCorrectIndex
            : 0;

        return {
            id: i,
            question: q.question || q.text,
            image: q.image || null,
            options,
            correctIndex
        };
    });
    testTakingCurrentIndex = 0;
    testTakingAnswers = {};
    testTakingHintUsed = {};
    testTakingHiddenAnswers = {};
    renderTestResultsBreakdown(false);
    
    hideAllPages();
    document.getElementById('testTakingPage')?.classList.remove('hidden');
    document.getElementById('testTakingTitle').textContent = testData.title || 'Тест';
    
    renderTestNavigation();
    showTestQuestion();
}

function renderTestNavigation() {
    const navBar = document.getElementById('testNavBar');
    if (!navBar) return;
    
    navBar.innerHTML = '';
    
    testTakingQuestions.forEach((q, i) => {
        const btn = document.createElement('button');
        btn.className = 'ai-nav-btn';
        if (i === testTakingCurrentIndex) btn.classList.add('active');
        if (testTakingAnswers[i] !== undefined) btn.classList.add('answered');
        btn.textContent = i + 1;
        btn.onclick = () => goToTestQuestion(i);
        navBar.appendChild(btn);
    });
}

function showTestQuestion() {
    const question = testTakingQuestions[testTakingCurrentIndex];
    if (!question) return;
    
    document.getElementById('testQuestionNumber').textContent = 
        `${t('quizQuestion')} ${testTakingCurrentIndex + 1}/${testTakingQuestions.length}`;

    const answerModeAtEnd = testTakingSettings.answerMode === 'end';
    const selectedAnswer = testTakingAnswers[testTakingCurrentIndex];
    const hiddenIndex = testTakingHiddenAnswers[testTakingCurrentIndex];

    const questionTextEl = document.getElementById('testQuestionText');
    if (questionTextEl) {
        questionTextEl.textContent = question.question;
    }

    const questionImageWrap = document.getElementById('testQuestionImageWrap');
    const questionImageEl = document.getElementById('testQuestionImage');
    if (questionImageWrap && questionImageEl) {
        if (question.image) {
            questionImageEl.src = question.image;
            questionImageEl.alt = t('questionImageAlt') || 'Question image';
            questionImageWrap.classList.remove('hidden');
        } else {
            questionImageEl.removeAttribute('src');
            questionImageWrap.classList.add('hidden');
        }
    }
    
    const answersContainer = document.getElementById('testAnswers');
    answersContainer.innerHTML = '';
    
    const letters = ['A', 'B', 'C', 'D'];
    question.options.forEach((opt, i) => {
        const btn = document.createElement('button');
        btn.className = 'ai-answer-btn';
        btn.textContent = `${letters[i]}) ${opt}`;

        if (hiddenIndex === i && selectedAnswer !== i) {
            btn.classList.add('hint-hidden');
            btn.disabled = true;
        }

        if (answerModeAtEnd) {
            if (selectedAnswer === i) {
                btn.classList.add('selected');
            }
        } else if (selectedAnswer !== undefined) {
            btn.disabled = true;
            if (selectedAnswer === i) {
                btn.classList.add('selected');
                if (i === question.correctIndex) {
                    btn.classList.add('correct');
                } else {
                    btn.classList.add('incorrect');
                }
            }
            if (i === question.correctIndex) {
                btn.classList.add('correct');
            }
        }
        
        btn.onclick = () => selectTestAnswer(i);
        answersContainer.appendChild(btn);
    });

    const hintBtn = document.getElementById('testHintBtn');
    if (hintBtn) {
        const canShow = testTakingSettings.hintsEnabled;
        hintBtn.classList.toggle('hidden', !canShow);
        if (canShow) {
            const disableHint = selectedAnswer !== undefined || !!testTakingHintUsed[testTakingCurrentIndex];
            hintBtn.disabled = disableHint;
            hintBtn.classList.toggle('used', disableHint);
        }
    }

    
    typesetMathIn(questionTextEl);
    typesetMathIn(answersContainer);
    
    
    document.getElementById('testPrevBtn').disabled = testTakingCurrentIndex === 0;
    
    const allAnswered = testTakingQuestions.every((_, i) => testTakingAnswers[i] !== undefined);
    document.getElementById('testNextBtn').classList.toggle('hidden', allAnswered && testTakingCurrentIndex === testTakingQuestions.length - 1);
    document.getElementById('testFinishBtn').classList.toggle('hidden', !allAnswered);
    
    renderTestNavigation();
}

function selectTestAnswer(answerIndex) {
    const question = testTakingQuestions[testTakingCurrentIndex];
    if (!question) return;

    if (testTakingHiddenAnswers[testTakingCurrentIndex] === answerIndex) {
        return;
    }

    const answerModeAtEnd = testTakingSettings.answerMode === 'end';
    const hadAnswer = testTakingAnswers[testTakingCurrentIndex] !== undefined;
    if (!answerModeAtEnd && hadAnswer) return;

    testTakingAnswers[testTakingCurrentIndex] = answerIndex;
    showTestQuestion();
    
    if (!hadAnswer) {
        setTimeout(() => {
            if (testTakingCurrentIndex < testTakingQuestions.length - 1) {
                nextTestQuestion();
            }
        }, answerModeAtEnd ? 250 : 800);
    }
}

function useTestHint() {
    if (!testTakingSettings.hintsEnabled) return;
    if (testTakingAnswers[testTakingCurrentIndex] !== undefined) return;
    if (testTakingHintUsed[testTakingCurrentIndex]) return;

    const question = testTakingQuestions[testTakingCurrentIndex];
    if (!question || !Array.isArray(question.options)) return;

    const candidates = question.options
        .map((_, index) => index)
        .filter(index => index !== question.correctIndex && index !== testTakingHiddenAnswers[testTakingCurrentIndex]);

    if (candidates.length === 0) return;

    const randomIndex = candidates[Math.floor(Math.random() * candidates.length)];
    testTakingHiddenAnswers[testTakingCurrentIndex] = randomIndex;
    testTakingHintUsed[testTakingCurrentIndex] = true;
    showTestQuestion();
}

function prevTestQuestion() {
    if (testTakingCurrentIndex > 0) {
        testTakingCurrentIndex--;
        showTestQuestion();
    }
}

function nextTestQuestion() {
    if (testTakingCurrentIndex < testTakingQuestions.length - 1) {
        testTakingCurrentIndex++;
        showTestQuestion();
    }
}

function goToTestQuestion(index) {
    testTakingCurrentIndex = index;
    showTestQuestion();
}

function finishTest() {
    let correct = 0;
    testTakingQuestions.forEach((q, i) => {
        if (testTakingAnswers[i] === q.correctIndex) correct++;
    });
    
    const total = testTakingQuestions.length;
    const percent = Math.round((correct / total) * 100);
    
    const scoreEl = document.getElementById('testResultsScore');
    if (scoreEl) {
        scoreEl.innerHTML = `
            <span class="ai-score-number">${correct}/${total}</span>
            <span class="ai-score-percent">${percent}%</span>
        `;
    }

    if (testTakingMaterial && !isOwnTest(testTakingMaterial)) {
        recordExternalTestCompletion();
    }

    renderTestResultsBreakdown(testTakingSettings.answerMode === 'end');
    
    openModal('testResultsModal');
}

function renderTestResultsBreakdown(showDetails) {
    const breakdown = document.getElementById('testResultsBreakdown');
    if (!breakdown) return;

    if (!showDetails) {
        breakdown.innerHTML = '';
        breakdown.classList.add('hidden');
        return;
    }

    let html = '';
    testTakingQuestions.forEach((question, index) => {
        const selectedIndex = testTakingAnswers[index];
        const selectedAnswer = selectedIndex !== undefined
            ? (question.options?.[selectedIndex] || (t('noAnswer') || 'No answer'))
            : (t('noAnswer') || 'No answer');
        const correctAnswer = question.options?.[question.correctIndex] || '-';
        const isCorrect = selectedIndex === question.correctIndex;

        html += `
            <div class="test-result-item ${isCorrect ? 'correct' : 'incorrect'}">
                <div class="test-result-question">${index + 1}. ${escapeHtml(question.question || '')}</div>
                <div class="test-result-meta">${escapeHtml(t('yourAnswer') || 'Your answer')}: ${escapeHtml(selectedAnswer)}</div>
                <div class="test-result-meta">${escapeHtml(t('correctAnswer') || 'Correct answer')}: ${escapeHtml(correctAnswer)}</div>
            </div>
        `;
    });

    breakdown.innerHTML = html;
    breakdown.classList.remove('hidden');
}

function closeTestResults() {
    closeModal('testResultsModal');
    renderTestResultsBreakdown(false);
    showLibrary();
}

function retakeTest() {
    closeModal('testResultsModal');
    testTakingCurrentIndex = 0;
    testTakingAnswers = {};
    testTakingHintUsed = {};
    testTakingHiddenAnswers = {};
    renderTestResultsBreakdown(false);
    showTestQuestion();
}

function confirmExitTest() {
    const answered = Object.keys(testTakingAnswers).length;
    if (answered > 0) {
        openModal('exitTestModal');
    } else {
        showLibrary();
    }
}

function forceExitTest() {
    closeModal('exitTestModal');
    showLibrary();
}

// ==================== BLOCK NAVIGATION IN TEST EDITOR ====================
function isInTestEditorPage() {
    const testEditorPage = document.getElementById('testEditorPage');
    return testEditorPage && !testEditorPage.classList.contains('hidden');
}

function handleBlockedNavigation() {
    if (isInTestEditorPage()) {
        showToast(t('saveOrExitEditor'), 'warning');
        return true;
    }
    return false;
}

// ==================== AI TEACHER INTEGRATION ====================

AI_TEACHER_API_URL = window.AI_TEACHER_API_URL || AI_TEACHER_API_URL;


const AITeacher = {
    currentMode: null,      
    mathMode: false,
    materialId: null,
    materialType: null,     
    selectedFile: null,
    historyMode: false,
    
    
    plan: [],
    currentSection: 0,
    currentView: 'general',
    sectionQuestions: [],
    questionQueue: [],
    learnSectionState: {},
    learnLeftSections: new Set(),
    learnVisitedSections: new Set(),
    learnQuestionsTotal: 0,
    
    
    flashcardsData: [],
    questionsData: [],
    previousQuestions: [],
    questionCount: 10,
    
    
    testCurrentIndex: 0,
    testAnswers: {},
    testHintUsed: {},
    testHiddenAnswers: {},
    isRealTest: false,
    showExplanations: true,
    
    
    flashcardIndex: 0,
    flashcardKnown: 0,
    flashcardFlipped: false,

    
    _inFlight: false
};

function setAITeacherInFlight(inFlight) {
    AITeacher._inFlight = inFlight;
    
    document.querySelectorAll('#aiQuestionCountModal .ai-count-btn').forEach(btn => {
        btn.disabled = inFlight;
        btn.classList.toggle('disabled', inFlight);
    });
}


function resolveAiApiEndpoint(path = '') {
    const base = String(AI_TEACHER_API_URL || '').trim().replace(/\/+$/, '');
    const normalizedPath = String(path || '').replace(/^\/+/, '');
    if (!base) {
        throw new Error('AI API URL is not configured');
    }
    return `${base}/${normalizedPath}`;
}

function normalizeAiApiError(error, fallbackMessage) {
    const rawMessage = String(error?.message || '').trim();
    const compactMessage = rawMessage.replace(/\s+/g, ' ').slice(0, 240);
    if (/request timeout|aborterror|signal is aborted|aborted/i.test(rawMessage)) {
        return `${fallbackMessage} (AI request timed out, retry or use a smaller PDF)`;
    }
    if (/failed to fetch|networkerror|load failed/i.test(rawMessage)) {
        return `${fallbackMessage} (Network/CORS/Cloudflare issue)`;
    }
    if (/<!doctype|<html|backend write error|varnish|error 54113|service unavailable|bad gateway|gateway timeout|http 50[234]/i.test(rawMessage)) {
        return `${fallbackMessage} (AI provider is temporarily unavailable, please retry shortly)`;
    }
    return compactMessage || fallbackMessage;
}

async function aiRequestWithRetry(factory, fallbackMessage, attempts = 2, delayMs = 900) {
    let lastError = null;
    for (let i = 1; i <= attempts; i++) {
        try {
            return await factory();
        } catch (error) {
            lastError = error;
            const message = String(error?.message || '').toLowerCase();
            const isTransient = /temporarily unavailable|timeout|timed out|gateway|network|fetch|connection|closed unexpectedly|502|503|504/.test(message);
            if (i < attempts && isTransient) {
                await new Promise(res => setTimeout(res, delayMs));
                continue;
            }
            break;
        }
    }
    throw new Error(normalizeAiApiError(lastError, fallbackMessage));
}

const AITeacherAPI = {
    async uploadMaterial(material) {
        const formData = new FormData();
        if (material instanceof File) {
            formData.append('file', material);
        } else {
            formData.append('text', material);
        }

        try {
            return await aiRequestWithRetry(
                () => fetchJsonWithControl(resolveAiApiEndpoint('upload'), {
                    method: 'POST',
                    body: formData,
                    timeoutMs: AI_UPLOAD_TIMEOUT_MS,
                    credentials: 'omit',
                    cache: 'no-store'
                }),
                t('uploadError') || 'Upload error'
            );
        } catch (error) {
            throw new Error(normalizeAiApiError(error, t('uploadError') || 'Upload error'));
        }
    },

    async generateLearn(materialId, historyMode = false) {
        try {
            return await aiRequestWithRetry(
                () => fetchJsonWithControl(resolveAiApiEndpoint('generate/learn'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ material_id: materialId, history_mode: historyMode, language: currentLang }),
                    timeoutMs: AI_GENERATE_LEARN_TIMEOUT_MS,
                    credentials: 'omit',
                    cache: 'no-store'
                }),
                t('generationError') || 'Generation error'
            );
        } catch (error) {
            throw new Error(normalizeAiApiError(error, t('generationError') || 'Generation error'));
        }
    },

    async generatePractice(materialId, count, excludeQuestions = []) {
        try {
            return await aiRequestWithRetry(
                () => fetchJsonWithControl(resolveAiApiEndpoint('generate/practice'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ material_id: materialId, count, exclude_questions: excludeQuestions, language: currentLang }),
                    timeoutMs: AI_GENERATE_QUIZ_TIMEOUT_MS,
                    credentials: 'omit',
                    cache: 'no-store'
                }),
                t('generationError') || 'Generation error'
            );
        } catch (error) {
            throw new Error(normalizeAiApiError(error, t('generationError') || 'Generation error'));
        }
    },

    async generateRealTest(materialId, count) {
        try {
            return await aiRequestWithRetry(
                () => fetchJsonWithControl(resolveAiApiEndpoint('generate/realtest'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ material_id: materialId, count, language: currentLang }),
                    timeoutMs: AI_GENERATE_QUIZ_TIMEOUT_MS,
                    credentials: 'omit',
                    cache: 'no-store'
                }),
                t('generationError') || 'Generation error'
            );
        } catch (error) {
            throw new Error(normalizeAiApiError(error, t('generationError') || 'Generation error'));
        }
    },

    async healthCheck() {
        try {
            await fetchJsonWithControl(resolveAiApiEndpoint('health'), {
                timeoutMs: 7000,
                credentials: 'omit',
                cacheTtlMs: 15 * 1000,
                cacheKey: 'ai-health'
            });
            return true;
        } catch {
            return false;
        }
    }
};


async function openAITeacherModal(mode) {
    if (!AI_TEACHER_API_URL) {
        try {
            await appInitPromise;
            AI_TEACHER_API_URL = window.AI_TEACHER_API_URL || AI_TEACHER_API_URL;
        } catch (error) {
            console.error('AI config preload failed:', error);
        }
    }

    if (!AI_TEACHER_API_URL) {
        showToast('AI сервері бапталмаған', 'error');
        return;
    }
    AITeacher.currentMode = mode;
    updateAIUsageUI();
    openModal('aiTeacherMaterialModal');
}

function selectAIMaterialType(type) {
    AITeacher.materialType = type;
    closeModal('aiTeacherMaterialModal');
    
    
    const showHistoryMode = AITeacher.currentMode === 'learn';
    
    if (type === 'text') {
        const historyToggleText = document.getElementById('aiHistoryModeToggle');
        if (historyToggleText) {
            historyToggleText.style.display = showHistoryMode ? 'flex' : 'none';
        }
        
        const historyCheckbox = document.getElementById('aiHistoryModeText');
        if (historyCheckbox) historyCheckbox.checked = false;
        
        openModal('aiTeacherTextModal');
        return;
    }

    if (type === 'pdf') {
        AITeacher.selectedFile = null;
        const pdfInput = document.getElementById('aiPdfInput');
        const selectedFile = document.getElementById('aiSelectedFile');
        const submitBtn = document.getElementById('aiSubmitPdfBtn');
        if (pdfInput) pdfInput.value = '';
        if (selectedFile) selectedFile.textContent = '';
        if (submitBtn) submitBtn.disabled = true;
        
        const historyTogglePdf = document.getElementById('aiHistoryModeTogglePdf');
        if (historyTogglePdf) {
            historyTogglePdf.style.display = showHistoryMode ? 'flex' : 'none';
        }
        
        const historyCheckboxPdf = document.getElementById('aiHistoryModePdf');
        if (historyCheckboxPdf) historyCheckboxPdf.checked = false;
        
        openModal('aiTeacherPdfModal');
        return;
    }
}

async function submitAIMaterial() {
    const text = document.getElementById('aiMaterialText')?.value?.trim();
    
    if (!text) {
        showToast(t('enterMaterial') || 'Материалды енгізіңіз', 'warning');
        return;
    }
    
    
    if (AITeacher.currentMode === 'learn') {
        const historyCheckbox = document.getElementById('aiHistoryModeText');
        AITeacher.historyMode = historyCheckbox ? historyCheckbox.checked : false;
    } else {
        AITeacher.historyMode = false;
    }
    
    closeModal('aiTeacherTextModal');
    await uploadAndProcessAIMaterial(text);
}

async function submitAIPdf() {
    if (!AITeacher.selectedFile) {
        showToast(t('selectPdf') || 'PDF файлын таңдаңыз', 'warning');
        return;
    }
    
    
    if (AITeacher.currentMode === 'learn') {
        const historyCheckbox = document.getElementById('aiHistoryModePdf');
        AITeacher.historyMode = historyCheckbox ? historyCheckbox.checked : false;
    } else {
        AITeacher.historyMode = false;
    }
    
    closeModal('aiTeacherPdfModal');
    await uploadAndProcessAIMaterial(AITeacher.selectedFile);
}

function handleAIFileSelect(event) {
    const file = event.target.files[0];
    if (file && file.type === 'application/pdf') {
        AITeacher.selectedFile = file;
        document.getElementById('aiSelectedFile').textContent = file.name;
        document.getElementById('aiSubmitPdfBtn').disabled = false;
    }
}

async function uploadAndProcessAIMaterial(material) {
    if (AITeacher._inFlight) return;
    setAITeacherInFlight(true);
    showAILoading();
    
    try {
        const result = await AITeacherAPI.uploadMaterial(material);
        AITeacher.materialId = result.material_id;
        
        hideAILoading();
        setAITeacherInFlight(false);
        
        if (AITeacher.currentMode === 'learn') {
            startAILearnMode();
        } else {
            openModal('aiQuestionCountModal');
        }
    } catch (error) {
        hideAILoading();
        setAITeacherInFlight(false);
        showToast(error.message, 'error');
    }
}

async function selectAIQuestionCount(count) {
    closeModal('aiQuestionCountModal');
    AITeacher.questionCount = count;
    
    
    if (AITeacher._continuing) {
        AITeacher._continuing = false;
        await continueAIPractice(count);
        return;
    }
    
    if (AITeacher.currentMode === 'practice') {
        await startAIPracticeMode();
    } else if (AITeacher.currentMode === 'realtest') {
        await startAIRealTestMode();
    }
}


async function continueAIPractice(count) {
    if (AITeacher._inFlight) return;
    if (!canUseAIOrWarn()) return;
    setAITeacherInFlight(true);
    showAILoading();
    
    try {
        const data = await AITeacherAPI.generatePractice(
            AITeacher.materialId, 
            count, 
            AITeacher.previousQuestions
        );
        recordAIUsage();
        
        AITeacher.flashcardsData = data.flashcards || [];
        AITeacher.questionsData = data.questions || [];
        AITeacher.previousQuestions.push(...AITeacher.questionsData.map(q => q.question));
        
        hideAILoading();
        
        if (AITeacher.flashcardsData.length > 0) {
            startAIFlashcards();
        } else {
            startAITest(false);
        }
    } catch (error) {
        hideAILoading();
        showToast(error.message, 'error');
    } finally {
        setAITeacherInFlight(false);
    }
}


function showAILoading() {
    document.getElementById('aiLoadingScreen')?.classList.add('active');
}

function hideAILoading() {
    document.getElementById('aiLoadingScreen')?.classList.remove('active');
}

// ==================== AI LEARN MODE ====================
async function startAILearnMode() {
    if (AITeacher._inFlight) return;
    if (!canUseAIOrWarn()) return;
    setAITeacherInFlight(true);
    showAILoading();
    
    try {
        const data = await AITeacherAPI.generateLearn(AITeacher.materialId, AITeacher.historyMode);
        recordAIUsage();

        AITeacher.mathMode = detectMathInValue(data.plan);
        AITeacher.plan = data.plan || [];
        AITeacher.currentSection = 0;
        AITeacher.currentView = 'general';
        initializeAILearnState();
        
        hideAILoading();
        
        if (AITeacher.plan.length === 0) {
            showToast(t('noPlanFound') || 'Оқу жоспары табылмады', 'error');
            return;
        }
        
        showAILearnPage();
    } catch (error) {
        hideAILoading();
        showToast(error.message, 'error');
    } finally {
        setAITeacherInFlight(false);
    }
}

function showAILearnPage() {
    hideAllPages();
    document.getElementById('aiLearnPage')?.classList.remove('hidden');
    renderAILearnPlan();
    showAILearnSection();
}

function initializeAILearnState() {
    AITeacher.learnSectionState = {};
    AITeacher.learnLeftSections = new Set();
    AITeacher.learnVisitedSections = new Set();
    AITeacher.learnQuestionsTotal = 0;

    AITeacher.plan.forEach((section, sectionIndex) => {
        const questions = Array.isArray(section.questions) ? section.questions : [];
        questions.forEach((question, questionIndex) => {
            if (!question._aiLearnId) {
                question._aiLearnId = `${sectionIndex}-${questionIndex}`;
            }
        });

        AITeacher.learnSectionState[sectionIndex] = {
            correctIds: new Set(),
            attemptedIds: new Set()
        };
        AITeacher.learnQuestionsTotal += questions.length;
    });

    AITeacher.learnVisitedSections.add(0);
}

function getAILearnSectionState(sectionIndex) {
    if (!AITeacher.learnSectionState[sectionIndex]) {
        AITeacher.learnSectionState[sectionIndex] = {
            correctIds: new Set(),
            attemptedIds: new Set()
        };
    }
    return AITeacher.learnSectionState[sectionIndex];
}

function getAILearnSectionStatus(sectionIndex) {
    const section = AITeacher.plan[sectionIndex];
    if (!section) return 'pending';

    const totalQuestions = Array.isArray(section.questions) ? section.questions.length : 0;
    if (totalQuestions === 0) return 'completed';

    const state = getAILearnSectionState(sectionIndex);
    const correctCount = state.correctIds.size;
    const attemptedCount = state.attemptedIds.size;

    if (correctCount >= totalQuestions) return 'completed';
    if (!AITeacher.learnLeftSections.has(sectionIndex)) return 'pending';
    if (attemptedCount === 0) return 'missed';
    return 'partial';
}

function getAILearnCompletedQuestionsCount() {
    return Object.values(AITeacher.learnSectionState).reduce((sum, state) => sum + state.correctIds.size, 0);
}

function updateAILearnProgress() {
    const progressFill = document.getElementById('aiLearnProgressFill');
    const progressText = document.getElementById('aiLearnProgressText');

    const total = AITeacher.learnQuestionsTotal || 0;
    const completed = getAILearnCompletedQuestionsCount();
    const percent = total > 0 ? (completed / total) * 100 : 0;

    if (progressFill) progressFill.style.width = `${percent}%`;
    if (progressText) progressText.textContent = `${completed}/${total}`;
}

function rebuildAILearnQuestionQueue(sectionIndex) {
    const section = AITeacher.plan[sectionIndex];
    if (!section) {
        AITeacher.sectionQuestions = [];
        AITeacher.questionQueue = [];
        return;
    }

    const questions = Array.isArray(section.questions) ? section.questions : [];
    const state = getAILearnSectionState(sectionIndex);
    AITeacher.sectionQuestions = questions;
    AITeacher.questionQueue = questions.filter(question => !state.correctIds.has(question._aiLearnId));
}

function markAILearnCurrentSectionLeft() {
    AITeacher.learnLeftSections.add(AITeacher.currentSection);
}

function renderAILearnPlan() {
    const container = document.getElementById('aiLearnPlanNav');
    if (!container) return;
    
    let html = '<div class="ai-plan-list">';
    AITeacher.plan.forEach((section, i) => {
        const status = getAILearnSectionStatus(i);
        html += `
            <button class="ai-plan-item ${i === AITeacher.currentSection ? 'active' : ''} ${status === 'completed' ? 'completed' : ''} ${status === 'partial' ? 'partial' : ''} ${status === 'missed' ? 'missed' : ''}" 
                    onclick="goToAILearnSection(${i})">
                <span class="ai-plan-number">${i + 1}</span>
                <span class="ai-plan-title">${section.title}</span>
            </button>
        `;
    });
    html += '</div>';
    container.innerHTML = html;
}

function goToAILearnSection(index) {
    if (index >= 0 && index < AITeacher.plan.length) {
        if (index !== AITeacher.currentSection) {
            markAILearnCurrentSectionLeft();
        }
        AITeacher.currentSection = index;
        AITeacher.learnVisitedSections.add(index);
        AITeacher.currentView = 'general';
        showAILearnSection();
        renderAILearnPlan();
    }
}

function showAILearnSection() {
    if (AITeacher.currentSection >= AITeacher.plan.length) {
        completeAILearn();
        return;
    }
    
    const section = AITeacher.plan[AITeacher.currentSection];
    
    updateAILearnProgress();
    
    
    renderAIViewTabs();
    
    
    renderAILearnContent(section);
    
    
    rebuildAILearnQuestionQueue(AITeacher.currentSection);
    renderAILearnQuestion();
    
    
    const prevBtn = document.getElementById('aiLearnPrevBtn');
    const nextBtn = document.getElementById('aiLearnNextBtn');
    if (prevBtn) prevBtn.disabled = AITeacher.currentSection === 0;
    if (nextBtn) nextBtn.textContent = AITeacher.currentSection === AITeacher.plan.length - 1 ? 
        (t('finish') || 'Аяқтау') : (t('next') || 'Келесі →');
    
    renderAILearnPlan();
}

function renderAIViewTabs() {
    const container = document.getElementById('aiLearnViewTabs');
    if (!container) return;
    
    if (!AITeacher.historyMode) {
        container.innerHTML = '';
        return;
    }
    
    container.innerHTML = `
        <button class="ai-view-tab ${AITeacher.currentView === 'general' ? 'active' : ''}" 
                onclick="switchAIView('general')">
            📖 ${t('generalInfo') || 'Жалпы ақпарат'}
        </button>
        <button class="ai-view-tab ${AITeacher.currentView === 'summary' ? 'active' : ''}" 
                onclick="switchAIView('summary')">
            📋 ${t('summary') || 'Конспект'}
        </button>
        <button class="ai-view-tab ${AITeacher.currentView === 'timeline' ? 'active' : ''}" 
                onclick="switchAIView('timeline')">
            📅 ${t('timeline') || 'Хронология'}
        </button>
    `;
}

function switchAIView(view) {
    AITeacher.currentView = view;
    renderAIViewTabs();
    renderAILearnContent(AITeacher.plan[AITeacher.currentSection]);
}

function renderAILearnContent(section) {
    const container = document.getElementById('aiLearnContent');
    if (!container) return;
    
    let html = `<h2 class="ai-section-title">${section.title}</h2>`;
    const content = section.content;
    
    if (AITeacher.historyMode && content && typeof content === 'object' && (content.general || content.summary || content.timeline)) {
        switch (AITeacher.currentView) {
            case 'general':
                html += content.general ? `<div class="ai-content-general">${content.general}</div>` : '<p class="ai-no-content">Ақпарат жоқ</p>';
                break;
            case 'summary':
                if (content.summary && Array.isArray(content.summary) && content.summary.length > 0) {
                    html += '<ul class="ai-content-summary">';
                    content.summary.forEach(item => { html += `<li>${item}</li>`; });
                    html += '</ul>';
                } else {
                    html += '<p class="ai-no-content">Конспект жоқ</p>';
                }
                break;
            case 'timeline':
                if (content.timeline && Array.isArray(content.timeline) && content.timeline.length > 0) {
                    html += '<div class="ai-content-timeline">';
                    content.timeline.forEach(item => {
                        html += `
                            <div class="ai-timeline-item">
                                <div class="ai-timeline-period">${item.period || item.year || ''}</div>
                                <div class="ai-timeline-event">${item.event || item.description || ''}</div>
                            </div>
                        `;
                    });
                    html += '</div>';
                } else {
                    html += '<p class="ai-no-content">Хронология жоқ</p>';
                }
                break;
        }
    } else {
        
        if (typeof content === 'string') {
            html += `<div class="ai-content-text">${content}</div>`;
        } else if (content?.type === 'text') {
            html += `<div class="ai-content-text">${content.data}</div>`;
        } else if (content?.type === 'list' && Array.isArray(content.data)) {
            html += '<ul class="ai-content-list">';
            content.data.forEach(item => { html += `<li>${item}</li>`; });
            html += '</ul>';
        } else if (content?.data) {
            html += `<div class="ai-content-text">${content.data}</div>`;
        }
    }
    
    container.innerHTML = html;
    applyMathDisplayToLearnAnswers(container);
    typesetMathIn(container);
}

function renderAILearnQuestion() {
    const container = document.getElementById('aiLearnQuestions');
    if (!container) return;
    
    if (AITeacher.questionQueue.length === 0) {
        container.innerHTML = `<p class="ai-all-answered">✓ ${t('allQuestionsAnswered') || 'Барлық сұрақтарға жауап берілді!'}</p>`;
        return;
    }
    
    const question = AITeacher.questionQueue[0];
    const answers = shuffleArray([question.correct, ...question.wrong]);
    
    let html = `
        <div class="ai-learn-question-card">
            <p class=\"ai-learn-question-text\">${wrapMathIfLikely(question.question)}</p>
            <div class="ai-learn-answers">
    `;
    
    answers.forEach(answer => {
        const escaped = answer.replace(/'/g, "\\'");
        html += `<button class="ai-learn-answer-btn" onclick="answerAILearnQuestion('${escaped}')">${answer}</button>`;
    });
    
    html += '</div></div>';
    container.innerHTML = html;
    applyMathDisplayToLearnAnswers(container);
    typesetMathIn(container);
}

function answerAILearnQuestion(answer) {
    const question = AITeacher.questionQueue[0];
    if (!question) return;

    const sectionState = getAILearnSectionState(AITeacher.currentSection);
    sectionState.attemptedIds.add(question._aiLearnId);
    const buttons = document.querySelectorAll('.ai-learn-answer-btn');
    
    buttons.forEach(btn => {
        btn.disabled = true;
        const rawValue = btn.dataset.raw || btn.textContent.trim();
        if (rawValue === question.correct) {
            btn.classList.add('correct');
        }
        if (rawValue === answer && answer !== question.correct) {
            btn.classList.add('incorrect');
        }
    });
    
    if (answer === question.correct) {
        sectionState.correctIds.add(question._aiLearnId);
        AITeacher.questionQueue.shift();
        updateAILearnProgress();
        renderAILearnPlan();
        setTimeout(() => renderAILearnQuestion(), 1000);
    } else {
        AITeacher.questionQueue.push(AITeacher.questionQueue.shift());
        renderAILearnPlan();
        
        if (question.explanation) {
            const container = document.getElementById('aiLearnQuestions');
            container.innerHTML += `<div class="ai-learn-explanation"><p>${question.explanation}</p></div>`;
        }
        
        setTimeout(() => renderAILearnQuestion(), 2500);
    }
}

function prevAILearnSection() {
    if (AITeacher.currentSection > 0) {
        markAILearnCurrentSectionLeft();
        AITeacher.currentSection--;
        AITeacher.learnVisitedSections.add(AITeacher.currentSection);
        AITeacher.currentView = 'general';
        showAILearnSection();
    }
}

function nextAILearnSection() {
    if (AITeacher.questionQueue.length > 0) {
        showToast(t('answerAllQuestions') || 'Алдымен барлық сұрақтарға жауап беріңіз', 'warning');
        return;
    }
    
    markAILearnCurrentSectionLeft();
    AITeacher.currentSection++;
    AITeacher.learnVisitedSections.add(AITeacher.currentSection);
    AITeacher.currentView = 'general';
    showAILearnSection();
}

function completeAILearn() {
    hideAllPages();
    document.getElementById('aiLearnCompletePage')?.classList.remove('hidden');
}

function startPracticeFromAILearn() {
    AITeacher.currentMode = 'practice';
    openModal('aiQuestionCountModal');
}

// ==================== AI PRACTICE MODE ====================
async function startAIPracticeMode() {
    if (AITeacher._inFlight) return;
    if (!canUseAIOrWarn()) return;
    setAITeacherInFlight(true);
    showAILoading();
    
    try {
        const data = await AITeacherAPI.generatePractice(AITeacher.materialId, AITeacher.questionCount);
        recordAIUsage();

        AITeacher.mathMode = detectMathInValue({ questions: data.questions, flashcards: data.flashcards });
        AITeacher.flashcardsData = data.flashcards || [];
        AITeacher.questionsData = data.questions || [];
        AITeacher.previousQuestions = AITeacher.questionsData.map(q => q.question);
        
        hideAILoading();
        
        
        
        startAITest(false);
    } catch (error) {
        hideAILoading();
        showToast(error.message, 'error');
    } finally {
        setAITeacherInFlight(false);
    }
}

// ==================== AI REAL TEST MODE ====================
async function startAIRealTestMode() {
    if (AITeacher._inFlight) return;
    if (!canUseAIOrWarn()) return;
    setAITeacherInFlight(true);
    showAILoading();
    
    try {
        const data = await AITeacherAPI.generateRealTest(AITeacher.materialId, AITeacher.questionCount);
        recordAIUsage();

        AITeacher.mathMode = detectMathInValue(data.questions);
        AITeacher.questionsData = data.questions || [];
        
        hideAILoading();
        startAITest(true);
    } catch (error) {
        hideAILoading();
        showToast(error.message, 'error');
    } finally {
        setAITeacherInFlight(false);
    }
}

// ==================== AI FLASHCARDS ====================
function startAIFlashcards() {
    AITeacher.flashcardIndex = 0;
    AITeacher.flashcardKnown = 0;
    AITeacher.flashcardFlipped = false;
    
    hideAllPages();
    document.getElementById('aiFlashcardsPage')?.classList.remove('hidden');
    showAIFlashcard();
}

function showAIFlashcard() {
    if (AITeacher.flashcardIndex >= AITeacher.flashcardsData.length) {
        startAITest(false);
        return;
    }
    
    const card = AITeacher.flashcardsData[AITeacher.flashcardIndex];
    const flashcard = document.getElementById('aiFlashcard');
    const frontText = document.getElementById('aiFlashcardFrontText');
    const backText = document.getElementById('aiFlashcardBackText');
    const progressText = document.getElementById('aiFlashcardProgressText');
    
    AITeacher.flashcardFlipped = false;
    if (flashcard) flashcard.classList.remove('flipped');
    if (frontText) frontText.textContent = card.front;
    if (backText) backText.textContent = card.back;
    if (progressText) progressText.textContent = `${AITeacher.flashcardIndex + 1}/${AITeacher.flashcardsData.length}`;
}

function flipAIFlashcard() {
    const flashcard = document.getElementById('aiFlashcard');
    AITeacher.flashcardFlipped = !AITeacher.flashcardFlipped;
    if (flashcard) flashcard.classList.toggle('flipped', AITeacher.flashcardFlipped);
}

function aiFlashcardAnswer(knew) {
    if (knew) AITeacher.flashcardKnown++;
    AITeacher.flashcardIndex++;
    showAIFlashcard();
}

// ==================== AI TEST ====================
function startAITest(isRealTest) {
    AITeacher.isRealTest = isRealTest;
    AITeacher.showExplanations = !isRealTest;
    AITeacher.testCurrentIndex = 0;
    AITeacher.testAnswers = {};
    AITeacher.testHintUsed = {};
    AITeacher.testHiddenAnswers = {};
    
    
    AITeacher.questionsData = AITeacher.questionsData.map((q, i) => ({
        ...q,
        id: i,
        shuffledAnswers: shuffleArray([q.correct, ...q.wrong])
    }));
    
    hideAllPages();
    document.getElementById('aiTestPage')?.classList.remove('hidden');
    
    const testTitle = document.getElementById('aiTestTitle');
    if (testTitle) testTitle.textContent = isRealTest ? (t('realTest') || 'Нақты тест') : (t('practiceTest') || 'Практика тест');
    
    renderAITestNavigation();
    showAITestQuestion();
}

function renderAITestNavigation() {
    const nav = document.getElementById('aiQuestionNav');
    if (!nav) return;
    
    nav.innerHTML = '';
    AITeacher.questionsData.forEach((q, i) => {
        const btn = document.createElement('button');
        btn.className = 'ai-nav-btn';
        btn.textContent = i + 1;
        btn.onclick = () => goToAIQuestion(i);
        nav.appendChild(btn);
    });
    
    updateAITestNavigation();
}

function updateAITestNavigation() {
    const buttons = document.querySelectorAll('.ai-nav-btn');
    buttons.forEach((btn, i) => {
        btn.classList.remove('active', 'answered', 'correct', 'incorrect');
        
        if (i === AITeacher.testCurrentIndex) btn.classList.add('active');
        
        const question = AITeacher.questionsData[i];
        if (AITeacher.testAnswers[question.id] !== undefined) {
            btn.classList.add('answered');
            if (!AITeacher.isRealTest) {
                btn.classList.add(AITeacher.testAnswers[question.id] === question.correct ? 'correct' : 'incorrect');
            }
        }
    });
}

function showAITestQuestion() {
    const question = AITeacher.questionsData[AITeacher.testCurrentIndex];
    
    document.getElementById('aiQuestionNumber').textContent = 
        `${t('question') || 'Сұрақ'} ${AITeacher.testCurrentIndex + 1}/${AITeacher.questionsData.length}`;
    const questionTextEl = document.getElementById('aiQuestionText');
    if (questionTextEl) questionTextEl.textContent = wrapMathIfLikely(question.question);
    
    renderAITestAnswers(question);
    typesetMathIn(questionTextEl);
    
    document.getElementById('aiPrevBtn').disabled = AITeacher.testCurrentIndex === 0;
    document.getElementById('aiNextBtn').disabled = AITeacher.testCurrentIndex === AITeacher.questionsData.length - 1;
    
    const hintBtn = document.getElementById('aiHintBtn');
    if (hintBtn) {
        hintBtn.style.display = AITeacher.isRealTest ? 'none' : 'block';
        hintBtn.disabled = AITeacher.testHintUsed[question.id] || AITeacher.testAnswers[question.id] !== undefined;
        hintBtn.classList.toggle('used', hintBtn.disabled);
    }
    
    updateAIExplanation();
    updateAITestNavigation();
}

function renderAITestAnswers(question) {
    const container = document.getElementById('aiAnswers');
    if (!container) return;
    
    container.innerHTML = '';
    const hiddenIndex = AITeacher.testHiddenAnswers[question.id];
    const selectedAnswer = AITeacher.testAnswers[question.id];
    
    question.shuffledAnswers.forEach((answer, i) => {
        const btn = document.createElement('button');
        btn.className = 'ai-answer-btn';
        btn.textContent = wrapMathIfLikely(answer);
        
        if (hiddenIndex === i) {
            btn.classList.add('hint-hidden');
            btn.disabled = true;
        }
        
        if (selectedAnswer === answer) {
            btn.classList.add('selected');
            if (!AITeacher.isRealTest) {
                btn.classList.add(answer === question.correct ? 'correct' : 'incorrect');
            }
        }
        
        if (!AITeacher.isRealTest && selectedAnswer && answer === question.correct) {
            btn.classList.add('correct');
        }
        
        if (selectedAnswer && !AITeacher.isRealTest) btn.disabled = true;
        
        btn.onclick = () => selectAIAnswer(answer);
        container.appendChild(btn);
    });
    typesetMathIn(container);
}

function selectAIAnswer(answer) {
    const question = AITeacher.questionsData[AITeacher.testCurrentIndex];
    if (AITeacher.testAnswers[question.id] !== undefined && !AITeacher.isRealTest) return;
    
    AITeacher.testAnswers[question.id] = answer;
    renderAITestAnswers(question);
    updateAIExplanation();
    updateAITestNavigation();
}

function updateAIExplanation() {
    const question = AITeacher.questionsData[AITeacher.testCurrentIndex];
    const container = document.getElementById('aiExplanationContainer');
    const text = document.getElementById('aiExplanationText');
    
    if (AITeacher.isRealTest || !AITeacher.showExplanations) {
        if (container) container.style.display = 'none';
        return;
    }
    
    const selectedAnswer = AITeacher.testAnswers[question.id];
    
    if (selectedAnswer && selectedAnswer !== question.correct && question.explanation) {
        if (text) text.textContent = wrapMathIfLikely(question.explanation);
        if (container) container.style.display = 'block';
        typesetMathIn(container);
    } else {
        if (container) container.style.display = 'none';
    }
}

function useAIHint() {
    const question = AITeacher.questionsData[AITeacher.testCurrentIndex];
    if (AITeacher.testHintUsed[question.id] || AITeacher.testAnswers[question.id] !== undefined) return;
    
    const wrongIndices = question.shuffledAnswers
        .map((a, i) => a !== question.correct ? i : -1)
        .filter(i => i !== -1 && i !== AITeacher.testHiddenAnswers[question.id]);
    
    if (wrongIndices.length > 0) {
        const randomIndex = wrongIndices[Math.floor(Math.random() * wrongIndices.length)];
        AITeacher.testHiddenAnswers[question.id] = randomIndex;
        AITeacher.testHintUsed[question.id] = true;
        
        renderAITestAnswers(question);
        
        const hintBtn = document.getElementById('aiHintBtn');
        if (hintBtn) {
            hintBtn.disabled = true;
            hintBtn.classList.add('used');
        }
    }
}

function goToAIQuestion(index) {
    if (index >= 0 && index < AITeacher.questionsData.length) {
        AITeacher.testCurrentIndex = index;
        showAITestQuestion();
    }
}

function prevAIQuestion() {
    if (AITeacher.testCurrentIndex > 0) {
        AITeacher.testCurrentIndex--;
        showAITestQuestion();
    }
}

function nextAIQuestion() {
    if (AITeacher.testCurrentIndex < AITeacher.questionsData.length - 1) {
        AITeacher.testCurrentIndex++;
        showAITestQuestion();
    }
}

function showAIResults() {
    let correct = 0;
    const details = [];
    
    AITeacher.questionsData.forEach((q, i) => {
        const userAnswer = AITeacher.testAnswers[q.id];
        const isCorrect = userAnswer === q.correct;
        if (isCorrect) correct++;
        
        details.push({
            number: i + 1,
            question: q.question,
            userAnswer: userAnswer || (t('noAnswer') || 'Жауап берілмеген'),
            correctAnswer: q.correct,
            isCorrect
        });
    });
    
    const total = AITeacher.questionsData.length;
    const percent = Math.round((correct / total) * 100);
    
    document.getElementById('aiScoreNumber').textContent = `${correct}/${total}`;
    document.getElementById('aiScorePercent').textContent = `${percent}%`;
    
    const breakdown = document.getElementById('aiResultsBreakdown');
    const actions = document.getElementById('aiResultsActions');
    
    if (AITeacher.isRealTest) {
        let html = '<div class="ai-results-list">';
        details.forEach(d => {
            html += `
                <div class="ai-result-item ${d.isCorrect ? 'correct' : 'incorrect'}">
                    <span class="ai-result-number">${d.number}.</span>
                    <div class="ai-result-content" style="flex: 1; padding: 0 10px;">
                        <div class="ai-result-question" style="font-weight: 500; margin-bottom: 5px;">${d.question}</div>
                        <div class="ai-result-answers" style="font-size: 0.9em;">
                            <div style="color: var(--text-secondary);">Сіздің жауап: <span class="user-ans" style="color: var(--text-primary); font-weight: 500;">${d.userAnswer}</span></div>
                            <div style="color: var(--color-success);">Дұрыс жауап: <span class="correct-ans" style="font-weight: 500;">${d.correctAnswer}</span></div>
                        </div>
                    </div>
                    <span class="ai-result-icon">${d.isCorrect ? '✓' : '✗'}</span>
                </div>
            `;
        });
        html += '</div>';
        if (breakdown) {
            breakdown.innerHTML = html;
            breakdown.style.display = 'block';
        }
        
        if (actions) {
            actions.innerHTML = `<button class="btn btn-primary" onclick="exitAITeacher()">🚪 ${t('exit') || 'Шығу'}</button>`;
        }
    } else {
        if (breakdown) breakdown.style.display = 'none';
        
        if (actions) {
            actions.innerHTML = `
                <button class="btn btn-primary" onclick="repeatAITest()">🔄 ${t('repeat') || 'Қайталау'}</button>
                <button class="btn btn-secondary" onclick="continueWithOtherAI()">➡️ ${t('continueOther') || 'Басқа сұрақтармен жалғастыру'}</button>
                <button class="btn btn-outline" onclick="exitAITeacher()">🚪 ${t('exit') || 'Шығу'}</button>
            `;
        }
    }
    
    hideAllPages();
    document.getElementById('aiResultsPage')?.classList.remove('hidden');
}

function repeatAITest() {
    if (AITeacher.flashcardsData.length > 0) {
        startAIFlashcards();
    } else {
        startAITest(false);
    }
}

async function continueWithOtherAI() {
    openModal('aiQuestionCountModal');
    AITeacher._continuing = true;
}

function exitAITeacher() {
    hideAllPages();
    showHome();
}

function confirmExitAITeacher() {
    if (confirm(t('exitConfirm') || 'Шығуды қалайсыз ба? Прогресс сақталмайды.')) {
        exitAITeacher();
    }
}


function shuffleArray(array) {
    const arr = [...array];
    for (let i = arr.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr;
}

// ==================== INITIALIZATION ====================
async function init() {
    
    loadUserPreferences();
    
    
    document.body.setAttribute('data-theme', currentTheme);
    applyTranslations();
    initEventListeners();
    renderFaqContent();
    if (sessionStorage.getItem('ozger_post_logout_reload') === '1') {
        sessionStorage.removeItem('ozger_post_logout_reload');
        setTimeout(() => showToast(t('logoutSuccess'), 'success', 2500), 50);
    }

    await appInitPromise;

    if (userAvatar) {
        updateAvatarUI(userAvatar);
    }

    await loadSession();
    
    
    if (currentUser) {
        
        await loadAllUserData();
        showHome();
    } else {
        showLanding();
    }
    
    
    setupAuthListener();

    
    setTimeout(checkPasswordResetMode, 100);
}

async function setupAuthListener() {
    
    let attempts = 0;
    while (!supabaseClient && attempts < 20) {
        await new Promise(resolve => setTimeout(resolve, 100));
        attempts++;
    }
    
    if (supabaseClient) {
        supabaseClient.auth.onAuthStateChange(async (event, session) => {
            console.log('Auth state changed:', event);
            if (session?.user) {
                currentUser = session.user;
                
                await loadAllUserData();
            } else {
                currentUser = null;
                userProfile = null;
                userAvatar = null;
            }
            updateAuthUI();

            
            checkPasswordResetMode();
        });
    }
}

document.addEventListener('DOMContentLoaded', init);














