# Overview

This Flask-based real estate platform, InBack/Clickback, provides cashback services for new construction property purchases in the Krasnodar region. It connects buyers with developers, offers property listings, streamlines user applications, and includes client relationship management tools. Key features include a smart property search, residential complex comparisons with interactive maps, user favorites, a manager dashboard for client and cashback tracking, and robust notification and document generation capabilities. The platform aims to capture market share through unique cashback incentives and an intuitive user experience.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Frontend Architecture

The frontend uses server-side rendered HTML with Jinja2 and TailwindCSS (CDN-based). Interactivity is provided by vanilla JavaScript, employing a modular, component-based approach. The design emphasizes a mobile-first, responsive experience with progressive enhancement, including features like smart search, real-time filtering, Yandex Maps integration, property comparison, and PDF generation. Typography uses **Manrope** font family from Google Fonts for a modern, professional appearance.

## Backend Architecture

The backend is built with Flask 2.3.3, following an MVC pattern with blueprints. SQLAlchemy serves as the ORM for PostgreSQL. Key architectural decisions include Flask-Login for session management and role-based access control (RBAC), robust security (password hashing, CSRF protection), and custom parsing for Russian address formats. The system supports phone verification and manager-to-client presentation delivery. The architecture uses a normalized Developers → Residential Complexes → Properties schema.

## Data Storage Solutions

PostgreSQL, hosted on Neon Database (serverless) and managed via SQLAlchemy 2.0.32, is the primary database. The schema includes Users, Managers, Properties, Residential Complexes, Developers, Districts, Favorites, Saved Searches, Applications, Deals, and Recommendations. Caching uses Flask-Caching. Performance optimizations include 17 indexes on `properties` and 7 on `residential_complexes`, with composite and partial indexes for hot query paths, enabling fast filtering and JOINs.

## Authentication & Authorization

The system supports three user types through a single Flask-Login system: Regular Users, Managers (prefixed `m_` in session ID), and Admins (prefixed `a_` in session ID). All user types inherit from `UserMixin` and use `login_user()` for authentication. The `load_user()` function checks prefixes to determine user type and load the appropriate model. Logout clears all session data, preventing cross-account access.

## Project Structure

The project follows a standard Flask structure with `app.py` as the entry point, `models.py` for SQLAlchemy models, and dedicated folders for deployment/utility scripts, static assets, Jinja2 templates, data files, and user-uploaded assets.

# External Dependencies

## Third-Party APIs

-   **SendGrid**: Transactional emails and notifications.
-   **OpenAI**: Smart search and content generation.
-   **Telegram Bot API**: User notifications and communication.
-   **Yandex Maps API**: Interactive maps, geocoding, and location visualization with Russian address support.
-   **OpenStreetMap/Nominatim**: Geocoding and address parsing (legacy/fallback).
-   **Google Analytics**: User behavior tracking.
-   **LaunchDarkly**: Feature flagging.
-   **SMS.ru, SMSC.ru**: Russian SMS services for phone verification.
-   **Chaport**: Chat widget for user communication.

## Web Scraping Infrastructure

-   `selenium`, `playwright`, `beautifulsoup4`, `undetected-chromedriver`: Used for automated data collection.

## PDF Generation

-   `weasyprint`, `reportlab`: Used for generating property detail sheets, comparison reports, and cashback calculations.

## Image Processing

-   `Pillow`: Used for image resizing, compression, WebP conversion, and QR code generation.

## Additional Services

-   **Replit Infrastructure**: Development and hosting.
-   **reCAPTCHA**: Spam and bot prevention.

# Recent Changes & Migrations

## Data Migration - Manager Account (October 22, 2025)

Successfully migrated all manager data from user-based tables to manager-specific tables:

- **Favorites Migration**: 6 properties + 3 residential complexes migrated from `favorite_properties`/`favorite_complexes` → `manager_favorite_properties`/`manager_favorite_complexes`
- **Comparisons Migration**: 4 properties + 3 complexes migrated from `user_comparisons` → `manager_comparisons`
- **API Fix**: Corrected class names in `/api/manager/dashboard/all` endpoint (`ManagerFavorite` → `ManagerFavoriteProperty`, `ManagerComplexFavorite` → `ManagerFavoriteComplex`)

All manager panel features (favorites, comparisons, dashboard) now work correctly with migrated data.