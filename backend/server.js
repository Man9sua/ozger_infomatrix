const express = require('express');
const helmet = require('helmet');
const cors = require('cors');
const rateLimit = require('express-rate-limit');
const session = require('express-session');
const cookieParser = require('cookie-parser');
const winston = require('winston');
const expressWinston = require('express-winston');
const { createClient } = require('@supabase/supabase-js');
const { body, validationResult } = require('express-validator');
const csurf = require('csurf');
const { createServer } = require('http');
const { Server } = require('socket.io');
require('dotenv').config();


const logger = winston.createLogger({
  level: process.env.LOG_LEVEL || 'info',
  format: winston.format.combine(
    winston.format.timestamp(),
    winston.format.json()
  ),
  transports: [
    new winston.transports.File({ filename: 'error.log', level: 'error' }),
    new winston.transports.File({ filename: 'combined.log' }),
  ],
});

if (process.env.NODE_ENV !== 'production') {
  logger.add(new winston.transports.Console({
    format: winston.format.simple(),
  }));
}


const supabaseUrl = process.env.SUPABASE_URL;
const supabaseAnonKey = process.env.SUPABASE_ANON_KEY;
const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

if (!supabaseUrl || !supabaseAnonKey) {
  console.error('Missing required Supabase environment variables');
  process.exit(1);
}

const supabase = createClient(supabaseUrl, supabaseServiceKey || supabaseAnonKey);




const app = express();
const server = createServer(app);
const io = new Server(server, {
  cors: {
    origin: true, 
    credentials: true
  }
});
const PORT = process.env.PORT || 3001;


app.use(helmet({
  contentSecurityPolicy: {
    directives: {
      defaultSrc: ["'self'"],
      scriptSrc: ["'self'", "'unsafe-inline'", "https://cdn.jsdelivr.net", "https://cdn.socket.io"],
      styleSrc: ["'self'", "'unsafe-inline'"],
      imgSrc: ["'self'", "data:", "https:"],
      fontSrc: ["'self'", "data:", "https://cdn.jsdelivr.net"],
      connectSrc: ["'self'", supabaseUrl],
      frameSrc: ["'none'"],
      objectSrc: ["'none'"],
      upgradeInsecureRequests: [],
    },
  },
  hsts: {
    maxAge: 31536000,
    includeSubDomains: true,
    preload: true
  }
}));


const limiter = rateLimit({
  windowMs: parseInt(process.env.RATE_LIMIT_WINDOW_MS) || 15 * 60 * 1000, 
  max: parseInt(process.env.RATE_LIMIT_MAX_REQUESTS) || 100,
  message: 'Too many requests from this IP, please try again later.',
  standardHeaders: true,
  legacyHeaders: false,
});
app.use(limiter);


app.use(cors({
  origin: true, 
  credentials: true,
  methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
  allowedHeaders: ['Content-Type', 'Authorization', 'X-Requested-With']
}));

app.use(expressWinston.logger({
  winstonInstance: logger,
  meta: true,
  msg: "HTTP {{req.method}} {{req.url}}",
  expressFormat: true,
  colorize: false,
}));


app.use(express.json({ limit: '10mb' }));
app.use(express.urlencoded({ extended: true, limit: '10mb' }));
app.use(cookieParser());


app.use(session({
  secret: process.env.SESSION_SECRET || 'your-super-secure-session-secret',
  resave: false,
  saveUninitialized: false,
  cookie: {
    secure: process.env.NODE_ENV === 'production',
    httpOnly: true,
    maxAge: 24 * 60 * 60 * 1000, 
    sameSite: 'strict'
  }
}));


const csrfProtection = csurf({
  cookie: {
    secure: process.env.NODE_ENV === 'production',
    httpOnly: true,
    sameSite: 'strict'
  }
});


const requireAuth = async (req, res, next) => {
  try {
    const token = req.cookies.supabase_auth_token || req.headers.authorization?.replace('Bearer ', '');

    if (!token) {
      return res.status(401).json({ error: 'No authentication token provided' });
    }

    const { data: { user }, error } = await supabase.auth.getUser(token);

    if (error || !user) {
      return res.status(401).json({ error: 'Invalid authentication token' });
    }

    req.user = user;
    next();
  } catch (error) {
    logger.error('Auth middleware error:', error);
    res.status(500).json({ error: 'Authentication error' });
  }
};


const authenticateSocket = async (socket, next) => {
  try {
    const token = socket.handshake.auth.token || socket.handshake.headers.authorization?.replace('Bearer ', '');

    if (!token) {
      return next(new Error('Authentication token required'));
    }

    const { data: { user }, error } = await supabase.auth.getUser(token);

    if (error || !user) {
      return next(new Error('Invalid authentication token'));
    }

    socket.user = user;
    next();
  } catch (error) {
    logger.error('Socket authentication error:', error);
    next(new Error('Authentication failed'));
  }
};


const connectedUsers = new Map();


io.use(authenticateSocket);

io.on('connection', (socket) => {
  const userId = socket.user.id;
  const username = socket.user.user_metadata?.username || socket.user.email;

  logger.info(`User connected: ${username} (${userId})`);

  
  connectedUsers.set(userId, {
    id: userId,
    username: username,
    email: socket.user.email,
    connectedAt: new Date().toISOString(),
    socketId: socket.id
  });

  
  socket.broadcast.emit('user_online', {
    userId: userId,
    username: username,
    timestamp: new Date().toISOString()
  });

  
  const onlineUsers = Array.from(connectedUsers.values()).filter(user => user.id !== userId);
  socket.emit('online_users', onlineUsers);

  
  socket.on('disconnect', () => {
    logger.info(`User disconnected: ${username} (${userId})`);

    
    connectedUsers.delete(userId);

    
    socket.broadcast.emit('user_offline', {
      userId: userId,
      username: username,
      timestamp: new Date().toISOString()
    });
  });

  
  socket.on('get_online_classmates', async () => {
    try {
      
      const { data: profile, error } = await supabase
        .from('profiles')
        .select('*')
        .eq('user_id', userId)
        .single();

      if (error && error.code !== 'PGRST116') {
        logger.error('Error fetching profile for classmates:', error);
        return;
      }

      if (profile && profile.school && profile.class_number) {
        
        const onlineClassmates = [];
        for (const [connectedUserId, userData] of connectedUsers) {
          if (connectedUserId === userId) continue;

          const { data: classmateProfile, error: classmateError } = await supabase
            .from('profiles')
            .select('*')
            .eq('user_id', connectedUserId)
            .single();

          if (!classmateError && classmateProfile &&
              classmateProfile.school === profile.school &&
              classmateProfile.class_number === profile.class_number) {
            onlineClassmates.push({
              ...userData,
              profile: classmateProfile
            });
          }
        }

        socket.emit('online_classmates', onlineClassmates);
      }
    } catch (error) {
      logger.error('Error getting online classmates:', error);
    }
  });

  
  socket.on('join_materials', () => {
    socket.join('materials_room');
    logger.info(`User ${username} joined materials room`);
  });

  
  socket.on('leave_materials', () => {
    socket.leave('materials_room');
    logger.info(`User ${username} left materials room`);
  });

  
  socket.on('join_favorites', () => {
    socket.join('favorites_room');
    logger.info(`User ${username} joined favorites room`);
  });

  
  socket.on('leave_favorites', () => {
    socket.leave('favorites_room');
    logger.info(`User ${username} left favorites room`);
  });

  
  socket.on('join_classroom', (classroomId) => {
    socket.join(`classroom_${classroomId}`);
    logger.info(`User ${username} joined classroom ${classroomId}`);
  });

  
  socket.on('leave_classroom', (classroomId) => {
    socket.leave(`classroom_${classroomId}`);
    logger.info(`User ${username} left classroom ${classroomId}`);
  });

  
  socket.on('material_created', async (data) => {
    try {
      
      io.to('materials_room').emit('new_material', {
        userId: userId,
        username: username,
        material: data,
        timestamp: new Date().toISOString()
      });

      logger.info(`Material created by ${username}: ${data.title}`);
    } catch (error) {
      logger.error('Material creation broadcast error:', error);
    }
  });

  
  socket.on('favorite_toggled', async (data) => {
    try {
      
      io.to('favorites_room').emit('favorite_changed', {
        userId: userId,
        username: username,
        materialId: data.materialId,
        isFavorited: data.isFavorited,
        timestamp: new Date().toISOString()
      });

      logger.info(`Favorite ${data.isFavorited ? 'added' : 'removed'} by ${username} for material ${data.materialId}`);
    } catch (error) {
      logger.error('Favorite toggle broadcast error:', error);
    }
  });

  
  socket.on('user_activity', async (data) => {
    try {
      
      const { data: profile, error } = await supabase
        .from('profiles')
        .select('*')
        .eq('user_id', userId)
        .single();

      if (!error && profile && profile.school && profile.class_number) {
        const classroomId = `${profile.school}_${profile.class_number}`;
        io.to(`classroom_${classroomId}`).emit('classmate_activity', {
          userId: userId,
          username: username,
          activity: data.activity,
          details: data.details,
          timestamp: new Date().toISOString()
        });
      }

      logger.info(`Activity ${data.activity} by ${username}`);
    } catch (error) {
      logger.error('User activity broadcast error:', error);
    }
  });

  
  socket.on('classroom_announcement', async (data) => {
    try {
      
      const { data: profile, error } = await supabase
        .from('profiles')
        .select('*')
        .eq('user_id', userId)
        .single();

      if (!error && profile && profile.school && profile.class_number) {
        const classroomId = `${profile.school}_${profile.class_number}`;
        io.to(`classroom_${classroomId}`).emit('announcement', {
          teacherId: userId,
          teacherName: username,
          message: data.message,
          timestamp: new Date().toISOString()
        });

        logger.info(`Announcement by ${username} in classroom ${classroomId}`);
      }
    } catch (error) {
      logger.error('Classroom announcement error:', error);
    }
  });

  
  socket.on('achievement_unlocked', async (data) => {
    try {
      
      const { data: profile, error } = await supabase
        .from('profiles')
        .select('*')
        .eq('user_id', userId)
        .single();

      if (!error && profile && profile.school && profile.class_number) {
        const classroomId = `${profile.school}_${profile.class_number}`;
        io.to(`classroom_${classroomId}`).emit('achievement_notification', {
          userId: userId,
          username: username,
          achievement: data.achievement,
          description: data.description,
          timestamp: new Date().toISOString()
        });
      }

      
      io.emit('global_achievement', {
        userId: userId,
        username: username,
        achievement: data.achievement,
        description: data.description,
        timestamp: new Date().toISOString()
      });

      logger.info(`Achievement unlocked by ${username}: ${data.achievement}`);
    } catch (error) {
      logger.error('Achievement broadcast error:', error);
    }
  });
});




app.get('/health', (req, res) => {
  res.json({ status: 'OK', timestamp: new Date().toISOString() });
});


app.get('/api/config', (req, res) => {
  res.json({
    supabaseUrl: process.env.SUPABASE_URL,
    supabaseAnonKey: process.env.SUPABASE_ANON_KEY,
    aiTeacherApiUrl: process.env.AI_TEACHER_API_URL || ''
  });
});


app.get('/csrf-token', csrfProtection, (req, res) => {
  res.json({ csrfToken: req.csrfToken() });
});


app.get('/api/online-users', requireAuth, (req, res) => {
  try {
    const onlineUsers = Array.from(connectedUsers.values()).filter(user => user.id !== req.user.id);
    res.json({ onlineUsers });
  } catch (error) {
    logger.error('Error fetching online users:', error);
    res.status(500).json({ error: 'Failed to fetch online users' });
  }
});


app.get('/api/notifications', requireAuth, async (req, res) => {
  try {
    
    
    res.json({ notifications: [] });
  } catch (error) {
    logger.error('Error fetching notifications:', error);
    res.status(500).json({ error: 'Failed to fetch notifications' });
  }
});


app.get('/api/classroom', requireAuth, async (req, res) => {
  try {
    const { data: profile, error } = await supabase
      .from('profiles')
      .select('*')
      .eq('user_id', req.user.id)
      .single();

    if (error && error.code !== 'PGRST116') {
      logger.error('Error fetching profile for classroom:', error);
      return res.status(500).json({ error: 'Failed to fetch profile' });
    }

    if (!profile || !profile.school || !profile.class_number) {
      return res.json({ classroom: null });
    }

    const classroomId = `${profile.school}_${profile.class_number}`;

    
    let onlineCount = 0;
    for (const [connectedUserId, userData] of connectedUsers) {
      if (connectedUserId === req.user.id) continue;

      const { data: classmateProfile, error: classmateError } = await supabase
        .from('profiles')
        .select('*')
        .eq('user_id', connectedUserId)
        .single();

      if (!classmateError && classmateProfile &&
          classmateProfile.school === profile.school &&
          classmateProfile.class_number === profile.class_number) {
        onlineCount++;
      }
    }

    res.json({
      classroom: {
        id: classroomId,
        school: profile.school,
        classNumber: profile.class_number,
        onlineCount: onlineCount,
        totalStudents: 25 
      }
    });
  } catch (error) {
    logger.error('Error fetching classroom info:', error);
    res.status(500).json({ error: 'Failed to fetch classroom info' });
  }
});


app.post('/auth/login', [
  body('email').isEmail().normalizeEmail(),
  body('password').isLength({ min: 6 })
], async (req, res) => {
  try {
    const errors = validationResult(req);
    if (!errors.isEmpty()) {
      return res.status(400).json({ errors: errors.array() });
    }

    const { email, password } = req.body;

    logger.info(`Login attempt for email: ${email}`);

    const { data, error } = await supabase.auth.signInWithPassword({
      email,
      password
    });

    if (error) {
      logger.warn(`Login failed for ${email}: ${error.message}`);
      return res.status(401).json({ error: 'Invalid credentials' });
    }

    
    res.cookie('supabase_auth_token', data.session.access_token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === 'production',
      sameSite: 'strict',
      maxAge: 24 * 60 * 60 * 1000 
    });

    logger.info(`Login successful for user: ${data.user.id}`);
    res.json({
      user: {
        id: data.user.id,
        email: data.user.email,
        username: data.user.user_metadata?.username
      }
    });
  } catch (error) {
    logger.error('Login error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});

app.post('/auth/register', [
  body('email').isEmail().normalizeEmail(),
  body('password').isLength({ min: 6 }),
  body('username').trim().isLength({ min: 2, max: 50 }).matches(/^[a-zA-Z0-9_]+$/),
  body('country').optional().isIn(['kz']),
  body('city').optional().isIn(['almaty', 'astana', 'shymkent', 'other'])
], csrfProtection, async (req, res) => {
  try {
    const errors = validationResult(req);
    if (!errors.isEmpty()) {
      return res.status(400).json({ errors: errors.array() });
    }

    const { email, password, username, country, city } = req.body;

    logger.info(`Registration attempt for email: ${email}, username: ${username}`);

    const { data, error } = await supabase.auth.signUp({
      email,
      password,
      options: {
        data: {
          username,
          country: country || 'kz',
          city: city || 'almaty'
        }
      }
    });

    if (error) {
      logger.warn(`Registration failed for ${email}: ${error.message}`);
      return res.status(400).json({ error: error.message });
    }

    logger.info(`Registration successful for user: ${data.user.id}`);
    res.json({
      message: 'Registration successful. Please check your email for verification.',
      user: {
        id: data.user.id,
        email: data.user.email,
        username: data.user.user_metadata?.username
      }
    });
  } catch (error) {
    logger.error('Registration error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});

app.post('/auth/logout', requireAuth, async (req, res) => {
  try {
    const { error } = await supabase.auth.signOut();

    if (error) {
      logger.warn(`Logout error for user ${req.user.id}: ${error.message}`);
    }

    
    res.clearCookie('supabase_auth_token');

    logger.info(`Logout successful for user: ${req.user.id}`);
    res.json({ message: 'Logged out successfully' });
  } catch (error) {
    logger.error('Logout error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});


app.get('/auth/me', requireAuth, (req, res) => {
  res.json({
    user: {
      id: req.user.id,
      email: req.user.email,
      username: req.user.user_metadata?.username,
      country: req.user.user_metadata?.country,
      city: req.user.user_metadata?.city
    }
  });
});


app.get('/api/materials', requireAuth, async (req, res) => {
  try {
    const { subject, type } = req.query;

    let query = supabase
      .from('materials')
      .select('*')
      .eq('user_id', req.user.id);

    if (subject && subject !== 'all') {
      query = query.eq('subject', subject);
    }

    if (type && type !== 'all') {
      query = query.eq('type', type);
    }

    const { data, error } = await query.order('created_at', { ascending: false });

    if (error) {
      logger.error('Error fetching materials:', error);
      return res.status(500).json({ error: 'Failed to fetch materials' });
    }

    res.json({ materials: data });
  } catch (error) {
    logger.error('Materials fetch error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});

app.post('/api/materials', [
  requireAuth,
  body('title').trim().isLength({ min: 1, max: 200 }),
  body('content').trim().isLength({ min: 1 }),
  body('subject').isIn(['history_kz', 'math_lit', 'reading', 'math', 'physics', 'chemistry', 'biology', 'geography', 'world_history', 'english', 'informatics', 'other']),
  body('type').isIn(['material', 'test']),
  body('is_public').isBoolean()
], async (req, res) => {
  try {
    const errors = validationResult(req);
    if (!errors.isEmpty()) {
      return res.status(400).json({ errors: errors.array() });
    }

    const { title, content, subject, type, is_public } = req.body;

    const { data, error } = await supabase
      .from('materials')
      .insert([{
        user_id: req.user.id,
        title,
        content,
        subject,
        type,
        is_public
      }])
      .select()
      .single();

    if (error) {
      logger.error('Error creating material:', error);
      return res.status(500).json({ error: 'Failed to create material' });
    }

    logger.info(`Material created by user ${req.user.id}: ${data.id}`);

    
    io.to('materials_room').emit('new_material', {
      userId: req.user.id,
      username: req.user.user_metadata?.username || req.user.email,
      material: data,
      timestamp: new Date().toISOString()
    });

    res.status(201).json({ material: data });
  } catch (error) {
    logger.error('Material creation error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});


app.get('/api/favorites', requireAuth, async (req, res) => {
  try {
    const { data, error } = await supabase
      .from('favorites')
      .select(`
        *,
        materials (*)
      `)
      .eq('user_id', req.user.id);

    if (error) {
      logger.error('Error fetching favorites:', error);
      return res.status(500).json({ error: 'Failed to fetch favorites' });
    }

    res.json({ favorites: data });
  } catch (error) {
    logger.error('Favorites fetch error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});

app.post('/api/favorites', requireAuth, async (req, res) => {
  try {
    const { material_id } = req.body;

    if (!material_id) {
      return res.status(400).json({ error: 'Material ID is required' });
    }

    
    const { data: existing, error: checkError } = await supabase
      .from('favorites')
      .select('*')
      .eq('user_id', req.user.id)
      .eq('material_id', material_id)
      .single();

    let result;
    let isFavorited;

    if (existing) {
      
      const { error: deleteError } = await supabase
        .from('favorites')
        .delete()
        .eq('user_id', req.user.id)
        .eq('material_id', material_id);

      if (deleteError) {
        logger.error('Error removing favorite:', deleteError);
        return res.status(500).json({ error: 'Failed to remove favorite' });
      }

      result = { message: 'Favorite removed' };
      isFavorited = false;
    } else {
      
      const { data, error: insertError } = await supabase
        .from('favorites')
        .insert([{
          user_id: req.user.id,
          material_id: material_id
        }])
        .select()
        .single();

      if (insertError) {
        logger.error('Error adding favorite:', insertError);
        return res.status(500).json({ error: 'Failed to add favorite' });
      }

      result = { favorite: data };
      isFavorited = true;
    }

    
    io.to('favorites_room').emit('favorite_changed', {
      userId: req.user.id,
      username: req.user.user_metadata?.username || req.user.email,
      materialId: material_id,
      isFavorited: isFavorited,
      timestamp: new Date().toISOString()
    });

    logger.info(`Favorite ${isFavorited ? 'added' : 'removed'} by user ${req.user.id} for material ${material_id}`);
    res.json(result);
  } catch (error) {
    logger.error('Favorite toggle error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});


app.get('/api/profile', requireAuth, async (req, res) => {
  try {
    const { data, error } = await supabase
      .from('profiles')
      .select('*')
      .eq('user_id', req.user.id)
      .single();

    if (error && error.code !== 'PGRST116') { 
      logger.error('Error fetching profile:', error);
      return res.status(500).json({ error: 'Failed to fetch profile' });
    }

    res.json({ profile: data });
  } catch (error) {
    logger.error('Profile fetch error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});

app.put('/api/profile', [
  requireAuth,
  body('username').optional().trim().isLength({ min: 2, max: 50 }).matches(/^[a-zA-Z0-9_]+$/),
  body('country').optional().isIn(['kz']),
  body('city').optional().isIn(['almaty', 'astana', 'shymkent', 'other']),
  body('school').optional().trim().isLength({ min: 2, max: 100 }),
  body('class_number').optional().isInt({ min: 1, max: 12 }),
  body('class_letter').optional().isIn(['А', 'Ә', 'Б', 'В', 'Г', 'Ғ', 'Д', 'Е', 'Ж', 'З', 'И', 'К', 'Л', 'М', 'Н', 'О', 'Ө', 'П', 'Р', 'С', 'Т', 'У', 'Ұ', 'Ү', 'Ф', 'Х', 'Ц', 'Ч', 'Ш', 'Щ', 'Ъ', 'Ы', 'І', 'Ь', 'Э', 'Ю', 'Я']),
  body('subject_combination').optional().isIn([
    'informatics-math',
    'geography-math',
    'physics-math',
    'biology-chemistry',
    'biology-geography',
    'history-english',
    'history-law',
    'creative'
  ]),
  body('subject1').optional().isIn(['math', 'physics', 'chemistry', 'biology', 'geography', 'world_history', 'english', 'informatics', 'law', 'creative']),
  body('subject2').optional().isIn(['math', 'physics', 'chemistry', 'biology', 'geography', 'world_history', 'english', 'informatics', 'law', 'creative']),
  body('avatar_url').optional().isString().isLength({ max: 200000 })
], async (req, res) => {
  try {
    const errors = validationResult(req);
    if (!errors.isEmpty()) {
      return res.status(400).json({ errors: errors.array() });
    }

    const updateData = {};
    const allowedFields = ['username', 'country', 'city', 'school', 'class_number', 'class_letter', 'subject_combination', 'subject1', 'subject2', 'avatar_url'];

    allowedFields.forEach(field => {
      if (req.body[field] !== undefined) {
        updateData[field] = req.body[field];
      }
    });

    const { data, error } = await supabase
      .from('profiles')
      .upsert({
        user_id: req.user.id,
        ...updateData,
        updated_at: new Date().toISOString()
      })
      .select()
      .single();

    if (error) {
      logger.error('Error updating profile:', error);
      return res.status(500).json({ error: 'Failed to update profile' });
    }

    logger.info(`Profile updated for user ${req.user.id}`);
    res.json({ profile: data });
  } catch (error) {
    logger.error('Profile update error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});




app.use((err, req, res, next) => {
  if (err.code === 'EBADCSRFTOKEN') {
    logger.warn(`CSRF token validation failed for ${req.ip}`);
    return res.status(403).json({ error: 'Invalid CSRF token' });
  }

  logger.error('Unhandled error:', err);
  res.status(500).json({ error: 'Internal server error' });
});


app.use('*', (req, res) => {
  res.status(404).json({ error: 'Route not found' });
});


server.listen(PORT, () => {
  logger.info(`Server running on port ${PORT} in ${process.env.NODE_ENV} mode`);
  console.log(`Server running on http://localhost:${PORT}`);
});

module.exports = app;


