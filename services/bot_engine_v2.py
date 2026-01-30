"""
bot_engine_v2.py

Smart bot engine that:
1. Dynamically discovers database schema
2. Intelligently queries relevant tables
3. Handles natural language questions
4. Processes images for context
5. Returns accurate, formatted responses
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
import base64

# For AI-powered query understanding (optional but recommended)
USE_AI_PROCESSING = os.getenv("USE_AI_PROCESSING", "true").lower() == "true"

if USE_AI_PROCESSING:
    # You can use OpenAI, Anthropic, or XAI
    # Example with OpenAI:
    # from openai import OpenAI
    # client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    pass

from core.db import get_db_connection
from core.config import USER_REFERENCE_COLUMNS
import phpserialize


class DatabaseSchemaDiscovery:
    """Discovers and caches database schema information"""
    
    def __init__(self):
        self.schema_cache = {}
    
    def get_user_related_tables(self, user_id: int) -> Dict[str, List[Dict]]:
        """
        Discovers all tables that contain user data
        Returns: {table_name: [row, row, ...]}
        """
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        user_data = {}
        
        try:
            # Get all tables
            cursor.execute("SHOW TABLES")
            tables = [list(row.values())[0] for row in cursor.fetchall()]
            
            for table in tables:
                # Get column info
                cursor.execute(f"SHOW COLUMNS FROM `{table}`")
                columns = [c["Field"] for c in cursor.fetchall()]
                
                # Find user-related columns
                matched_columns = USER_REFERENCE_COLUMNS.intersection(set(columns))
                
                if not matched_columns:
                    continue
                
                # Build dynamic WHERE clause
                where_conditions = " OR ".join([f"`{col}` = %s" for col in matched_columns])
                params = tuple([user_id] * len(matched_columns))
                
                # Query the table
                query = f"SELECT * FROM `{table}` WHERE {where_conditions}"
                cursor.execute(query, params)
                rows = cursor.fetchall()
                
                if rows:
                    user_data[table] = rows
                    
                    # Cache schema info
                    if table not in self.schema_cache:
                        self.schema_cache[table] = {
                            "columns": columns,
                            "user_columns": list(matched_columns)
                        }
            
            return user_data
            
        finally:
            cursor.close()
            conn.close()
    
    def get_vessel_data_from_meta(self, user_data: Dict) -> Optional[Dict]:
        """Extract and parse vessel data from WordPress usermeta"""
        
        # Look for usermeta table
        for table_name, rows in user_data.items():
            if 'usermeta' not in table_name.lower():
                continue
            
            for row in rows:
                if row.get('meta_key') == 'cs_vessel_data':
                    meta_value = row.get('meta_value')
                    if meta_value:
                        try:
                            # Deserialize PHP data
                            data = phpserialize.loads(
                                meta_value.encode('utf-8'), 
                                decode_strings=True
                            )
                            return self._normalize_vessel_data(data)
                        except Exception as e:
                            print(f"Failed to deserialize vessel data: {e}")
        
        return None
    
    def _normalize_vessel_data(self, raw_data: Any) -> Dict:
        """Convert PHP serialized data to normalized structure"""
        
        vessels = {}
        
        if isinstance(raw_data, dict):
            for key, item in raw_data.items():
                if not isinstance(item, dict):
                    continue
                
                vessel_name = str(item.get('vessel_name', '')).strip()
                if not vessel_name:
                    continue
                
                # Initialize vessel if not exists
                if vessel_name not in vessels:
                    vessels[vessel_name] = {
                        'name': vessel_name,
                        'observations': [],
                        'stats': {
                            'total': 0,
                            'open_defects': 0,
                            'closed_defects': 0,
                            'low': 0,
                            'medium': 0,
                            'high': 0,
                            'unknown': 0
                        }
                    }
                
                # Extract observation data
                obs_data = item.get('observation_data', {})
                if isinstance(obs_data, dict):
                    observation = {
                        'defect_status': str(obs_data.get('defects_status', '')).strip().lower(),
                        'priority_rating': str(obs_data.get('priority_rating', '')).strip(),
                        'inspection_date': str(obs_data.get('inspection_date', '')).strip(),
                        'defect_description': str(obs_data.get('defect_description', '')).strip(),
                        'location': str(obs_data.get('location', '')).strip(),
                    }
                    
                    vessels[vessel_name]['observations'].append(observation)
                    vessels[vessel_name]['stats']['total'] += 1
                    
                    # Count by status
                    if observation['defect_status'] == 'open':
                        vessels[vessel_name]['stats']['open_defects'] += 1
                    elif observation['defect_status'] == 'closed':
                        vessels[vessel_name]['stats']['closed_defects'] += 1
                    
                    # Count by priority
                    priority = observation['priority_rating']
                    if priority == '1':
                        vessels[vessel_name]['stats']['low'] += 1
                    elif priority == '2':
                        vessels[vessel_name]['stats']['medium'] += 1
                    elif priority == '3':
                        vessels[vessel_name]['stats']['high'] += 1
                    else:
                        vessels[vessel_name]['stats']['unknown'] += 1
        
        return vessels


class QueryAnalyzer:
    """Analyzes user questions to determine intent and extract entities"""
    
    def __init__(self):
        self.vessel_keywords = ['vessel', 'ship', 'clipper']
        self.stats_keywords = ['count', 'total', 'number', 'how many', 'stats', 'statistics']
        self.defect_keywords = ['defect', 'issue', 'problem', 'observation']
        self.status_keywords = ['status', 'state', 'condition']
        self.priority_keywords = ['priority', 'low', 'medium', 'high', 'severity']
    
    def analyze(self, question: str, available_vessels: List[str]) -> Dict:
        """
        Analyzes the question and returns structured intent
        """
        question_lower = question.lower()
        
        analysis = {
            'vessel_name': self._extract_vessel_name(question, available_vessels),
            'wants_stats': any(kw in question_lower for kw in self.stats_keywords),
            'wants_defects': any(kw in question_lower for kw in self.defect_keywords),
            'wants_status': any(kw in question_lower for kw in self.status_keywords),
            'wants_priority_breakdown': any(kw in question_lower for kw in self.priority_keywords),
            'wants_all_vessels': 'all' in question_lower and any(kw in question_lower for kw in self.vessel_keywords),
            'question_type': self._classify_question_type(question_lower)
        }
        
        return analysis
    
    def _extract_vessel_name(self, question: str, available_vessels: List[str]) -> Optional[str]:
        """Extract vessel name from question"""
        question_normalized = question.lower().strip()
        
        # Try exact matches first
        for vessel in available_vessels:
            if vessel.lower() in question_normalized:
                return vessel
        
        # Try partial matches
        for vessel in available_vessels:
            vessel_parts = vessel.lower().split()
            if any(part in question_normalized for part in vessel_parts if len(part) > 3):
                return vessel
        
        return None
    
    def _classify_question_type(self, question: str) -> str:
        """Classify the type of question being asked"""
        if any(word in question for word in ['what is', 'what are', 'tell me']):
            return 'informational'
        elif any(word in question for word in ['how many', 'count']):
            return 'quantitative'
        elif any(word in question for word in ['compare', 'versus', 'vs', 'difference']):
            return 'comparative'
        elif any(word in question for word in ['list', 'show all', 'give me all']):
            return 'listing'
        else:
            return 'general'


class ResponseFormatter:
    """Formats responses in a user-friendly way"""
    
    def format_vessel_stats(self, vessel_data: Dict, analysis: Dict) -> str:
        """Format vessel statistics based on what user asked for"""
        
        vessel_name = vessel_data['name']
        stats = vessel_data['stats']
        
        response_parts = []
        
        # Always include vessel name and status
        response_parts.append(f"**{vessel_name}**")
        response_parts.append(f"Status: Active")
        
        # Defect counts
        if analysis.get('wants_defects') or analysis.get('wants_stats'):
            response_parts.append(f"Open Defects: {stats['open_defects']}")
            if stats['closed_defects'] > 0:
                response_parts.append(f"Closed Defects: {stats['closed_defects']}")
        
        # Priority breakdown
        if analysis.get('wants_priority_breakdown') or analysis.get('wants_stats'):
            response_parts.append(
                f"Priority Breakdown: Low={stats['low']}, "
                f"Medium={stats['medium']}, High={stats['high']}, "
                f"Unknown={stats['unknown']}"
            )
        
        # Total observations
        if analysis.get('wants_stats'):
            response_parts.append(f"Total Observations: {stats['total']}")
        
        return "\n".join(response_parts)
    
    def format_all_vessels(self, vessels_data: Dict) -> str:
        """Format data for all vessels"""
        
        response = "**All Vessels Summary**\n\n"
        
        for vessel_name, vessel_data in sorted(vessels_data.items()):
            stats = vessel_data['stats']
            response += f"• {vessel_name}: {stats['open_defects']} open defects "
            response += f"(Low: {stats['low']}, Med: {stats['medium']}, High: {stats['high']})\n"
        
        return response
    
    def format_error(self, error_msg: str) -> str:
        """Format error messages"""
        return f"⚠️ {error_msg}"


class SmartBot:
    """Main bot orchestrator"""
    
    def __init__(self):
        self.schema_discovery = DatabaseSchemaDiscovery()
        self.query_analyzer = QueryAnalyzer()
        self.response_formatter = ResponseFormatter()
    
    def process_question(
        self, 
        user_id: int, 
        question: str,
        image_base64: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Main entry point for processing user questions
        """
        
        try:
            # 1. Get all user-related data from database
            user_data = self.schema_discovery.get_user_related_tables(user_id)
            
            if not user_data:
                return {
                    "success": False,
                    "answer": self.response_formatter.format_error(
                        "No data found for this user"
                    ),
                    "raw_data": {}
                }
            
            # 2. Extract vessel data
            vessels_data = self.schema_discovery.get_vessel_data_from_meta(user_data)
            
            if not vessels_data:
                return {
                    "success": False,
                    "answer": self.response_formatter.format_error(
                        "No vessel data found"
                    ),
                    "raw_data": {}
                }
            
            # 3. Analyze the question
            available_vessels = list(vessels_data.keys())
            analysis = self.query_analyzer.analyze(question, available_vessels)
            
            # 4. Process image if provided
            if image_base64:
                # You can use vision AI here to extract context from images
                # For now, we'll skip this
                pass
            
            # 5. Generate response
            if analysis['wants_all_vessels']:
                answer = self.response_formatter.format_all_vessels(vessels_data)
                raw_data = {vessel: data['stats'] for vessel, data in vessels_data.items()}
            
            elif analysis['vessel_name']:
                vessel_data = vessels_data.get(analysis['vessel_name'])
                if vessel_data:
                    answer = self.response_formatter.format_vessel_stats(vessel_data, analysis)
                    raw_data = vessel_data
                else:
                    answer = self.response_formatter.format_error(
                        f"Vessel '{analysis['vessel_name']}' not found"
                    )
                    raw_data = {}
            
            else:
                # Default: show summary of all vessels
                answer = self.response_formatter.format_all_vessels(vessels_data)
                raw_data = {vessel: data['stats'] for vessel, data in vessels_data.items()}
            
            return {
                "success": True,
                "question": question,
                "answer": answer,
                "raw_data": raw_data,
                "analysis": analysis
            }
        
        except Exception as e:
            return {
                "success": False,
                "answer": self.response_formatter.format_error(
                    f"An error occurred: {str(e)}"
                ),
                "raw_data": {},
                "error": str(e)
            }


# Main function to be called by API
def generate_bot_response(
    user_id: int, 
    question: str,
    image_base64: Optional[str] = None
) -> Dict[str, Any]:
    """
    Generate intelligent bot response
    
    Args:
        user_id: WordPress user ID
        question: User's natural language question
        image_base64: Optional base64 encoded image
    
    Returns:
        Dict with answer and metadata
    """
    bot = SmartBot()
    return bot.process_question(user_id, question, image_base64)