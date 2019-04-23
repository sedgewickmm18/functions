# *****************************************************************************
# Â© Copyright IBM Corp. 2018.  All Rights Reserved.
#
# This program and the accompanying materials
# are made available under the terms of the Apache V2.0
# which accompanies this distribution, and is available at
# http://www.apache.org/licenses/LICENSE-2.0
#
# *****************************************************************************

'''
Warning: The pipeline module is not a public API. These low level classes
should not be used directly. They are used from inside the method of 
public classes.

'''

import logging
import json
import re
import datetime as dt
import numpy as np
import time
import sys
import traceback
import timer
from collections import OrderedDict

import ibm_db
from . import dbhelper
from .enginelog import EngineLogging
from .util import log_df_info, freq_to_timedelta, StageException

import pandas as pd
import warnings
from pandas.api.types import (is_bool_dtype, is_numeric_dtype, is_string_dtype,
                              is_datetime64_any_dtype)
from sqlalchemy import (Table, Column, Integer, SmallInteger, String,
                        DateTime, Float, and_, func, select)

DATA_ITEM_TYPE_BOOLEAN = 'BOOLEAN'
DATA_ITEM_TYPE_NUMBER = 'NUMBER'
DATA_ITEM_TYPE_LITERAL = 'LITERAL'
DATA_ITEM_TYPE_TIMESTAMP = 'TIMESTAMP'

DATA_ITEM_COLUMN_TYPE_KEY = 'columnType'
DATA_ITEM_TRANSIENT_KEY = 'transient'
DATA_ITEM_SOURCETABLE_KEY = 'sourceTableName'

KPI_ENTITY_ID_COLUMN = 'ENTITY_ID'
KPI_TIMESTAMP_COLUMN = 'TIMESTAMP'

DATAFRAME_INDEX_ENTITYID = 'id'
DEFAULT_DATAFRAME_INDEX_TIMESTAMP = 'timestamp'


logger = logging.getLogger(__name__)


class DataAggregator(object):
    '''
    Default simple aggregation stage. 
    
    Parameters:
    -----------
    
    granularity: Granularity object
    
    agg_dict: dict
        Pandas aggregation dictionary
        
    complex_aggregators: list
        List of AS complex aggregation functions
        AS aggregation functions have an execute method that can be called
        inside of a pandas apply() on a groupby() to create a dataframe or series
    
    '''
    
    is_system_function = True
    _allow_empty_df = False
    _discard_prior_on_merge = True

    def __init__(self,name,granularity,agg_dict,complex_aggregators,input_items,
                 output_items):
        
        self.name = name
        self._input_set = input_items
        self._output_list = output_items
        self._agg_dict = agg_dict
        self._granularity = granularity
        self._complex_aggregators = complex_aggregators
        
    def __str__(self):
        
        msg = 'Aggregator: %s with granularity: %s. ' %(self.name,
                                                        self._granularity.name)
        for key,value in list(self._agg_dict.items()):
            msg = msg + ' Aggregates %s using %s .' %(key,value)
        for s in self._complex_aggregators:
            msg = msg + ' Uses %s to produce %s .' %(s.name,s.get_output_list())
            
        return msg
        
        
    def execute(self,df=None):
        
        gfs = []
        group = df.groupby(self._granularity.grouper)
        
        if not self._agg_dict is None and self._agg_dict:
            gf = group.agg(self._agg_dict)
            gfs.append(gf)
        for s in self._complex_aggregators:
            gf = group.apply(s.execute)
            gfs.append(gf)
        df = pd.concat(gfs,axis=1)
        
        df.columns = self._output_list
        
        logger.info('Completed aggregation: %s', self._granularity.name)
        return df
        
    def get_input_set(self):
        
        return self._input_set
        
    def get_output_list(self):
        
        return self._output_list

class DataMerge(object):
    '''
    A DataMerge object combines the results of execution of a stage
    with the results of execution of the previous stages.
    
    By default, a DataMerge object initializes itself with an empty
    dataframe. Although the main purpose of the DataMerge object is
    maintaining a dataframe, it can also keep track of any constants added
    during job processing so that it can re-apply constants if needed.
    
    Use the execute method to combine a new incoming data object with
    whatever data is present in the DataMerge at the time.
    
    '''
    
    is_system_function = True
    r_suffix = '_new_'
    
    def __init__ (self,name=None,df=None, **kwargs):
        
        if name is None:
            name = self.__class__.__name__
        self.name = name
        if df is None:
            df = pd.DataFrame()
        self.df = df
        self.constants = kwargs.get('constants',None)
        if self.constants is None:
            self.constants = {}
        
    def add_constant(self,name,value):
        '''
        Register a constant provide a value.
        Apply the constant to the dataframe.
        '''
        
        self.constants[name] = value
        self.df[name] = value
        
    def apply_constants(self):
        '''
        Apply the values of all constants to the dataframe.
        '''
        for name,value in list(self.constants.items()):
            self.df[name] = value
            
    def clear_data(self):
        '''
        Clear dataframe and constants
        '''
        
        self.constants = {}
        self.df = pd.DataFrame()
        
    def coalesce_cols(self,df,suffix):
        '''
        Combine two variants of the same column into a single. Variants are
        distinguished using a suffix, e.g. 'x' and 'x_new_' will be combined
        if the suffix of '_new_' is used. The coalesced result will be 
        placed in column 'x' and will contain 'x_new' where a value of 'x_new'
        was provided and 'x' where the value of 'x_new' was null.
        '''
        
        altered = []
        for i,o in enumerate(df.columns):
            try:
                drop = "%s%s" %(o,suffix)
                df[o] = df[o].fillna(df[drop])
                altered.append(drop)
            except KeyError:
                pass
        if len(altered) > 0:
            cols = [x for x in list(df.columns) if x not in altered]
            df = df[cols]
            
        return df
    
    def convert_to_df(self,obj,col_names,index):
        
        df = pd.DataFrame(data=obj,columns=col_names)
        df.index = index
        return df
    
    
    def get_index_names(self,df =None):
        
        '''
        Get a list of index names from a dataframe with a single index
        or multi-index.
        '''
        
        if df is None:
            df = self.df
            
        if df.index.name is not None:
            df_index_names = [df.index.name]
        else:
            df_index_names = list(df.index.names)
        df_index_names = [x for x in df_index_names if x is not None]
        
        return df_index_names
    
    def get_cols(self,df=None):
        
        '''
        Get a full set of column names from df and index. Return set.
        '''
        
        if df is None:
            df = self.df
        
        cols = set(self.get_index_names(df))
        cols |= set(df.columns)
        
        return cols
        

    def execute(self,obj,col_names,force_overwrite=False):
        '''
        Perform a smart merge between a dataframe and another object. The other
        object may be a dataframe, series, numpy array, list or scalar.
        
        Auto merge will choose between one of 3 merge strategies:
        1) slice when indexes are indentical
        2) full outer join when indexes are structured the same way
        3) lookup when obj has a single part index that matches one of the df cols
    
        col_names is a list of string data item names. This list of columns
        will be added or replaced during merge. When obj is a constant,
        tabular array, or series these column names these column names are
        neccessary to identify the contents. If the obj is a dataframe, if
        col_names are provided they will be used to rename the obj columns
        before merging.
    
        '''
        if obj is None:
            raise MergeError(('DataMerge is attempting to merge a null'
                                ' object with a dataframe. Object to be '
                                ' merged must be a dataframe, series,'
                                ' constant or numpy array. Unable to merge'
                                ' None'))
 
        existing = 'df'       
        
        if self.df is None:
            self.df = pd.DataFrame()
            
        if len(self.df.index)>0:
            logger.debug(('Input dataframe has columns %s and index %s'), 
                     list(self.df.columns),
                     self.get_index_names())
            existing = 'empty df'
            
        job_constants = list(self.constants.keys())
        
        if len(job_constants) > 0:
            logger.debug(('The job has constant output items %s'), 
                     [x for x in job_constants])        
            
        if isinstance(obj, (dict,OrderedDict)):
            raise MergeError(('Function error.'
                             ' A failure occured when attempting to merge a'
                             ' dictionary with a dataframe. Convert the dictionary'
                             ' to a dataframe or series and provide appropriate'
                             ' index names.')
                            )
        
        # if the object is a 2d array, convert to dataframe    
        if (len(col_names)> 1 and 
           obj is not None and 
           not isinstance(obj,(pd.DataFrame,pd.Series))):
            try:
                obj = self.convert_to_df(obj,
                                         col_names=col_names,
                                         index=self.df.index)
            except Exception:
                raise
            
        if isinstance(obj,(pd.DataFrame,pd.Series)):
            self.merge_dataframe(df=obj,
                                 col_names= col_names,
                                 force_overwrite=force_overwrite)
          
        else: 
            logger.debug((
                    'Merging dataframe with object of type %s'
                    ),type(obj))             
            self.merge_non_dataframe(obj,col_names = col_names)

            
        #test that df has expected columns
        df_cols = self.get_cols()
        if not self.df.empty and not set(col_names).issubset(df_cols):
            raise MergeError( ('Error in auto merge. Resulting df does not '
                    ' contain the expected output columns %s that should have'
                    ' been delivered through merge. It has columns %s'
                    %(col_names, df_cols)
                    ))
            
        return 'existing %s with new %s' %(existing,obj.__class__.__name__)
    
    def merge_dataframe(self,df,col_names,force_overwrite=True):
                
        #convert series to dataframe
        #rename columns as appropriate using supplied col_names
        if isinstance(df,pd.Series):
            if col_names is not None:
                df.name = col_names[0]
            else:
                col_names = [df.name]
            df = df.to_frame()
        else:
            if (col_names is None):
                col_names = list(df.columns)
            else:
                if len(col_names) == len(df.columns):
                    df.columns = col_names
        if len(df.index)>0:
            logger.debug((
                'Merging dataframe with columns %s and index %s'), 
                list(self.df.columns),
                self.get_index_names())
                    
        # profile incoming df to understand its structure
        # and determine merge strategy
        obj_index_names = self.get_index_names(df)
        merge_strategy = None
        if len(df.index)==0:
            merge_strategy = 'skip'
            logger.debug('Skipping empty dataframe received as merge input')
        elif len(self.df.index)==0:
            merge_strategy = 'replace'
        elif df.index.equals(self.df.index):
            if set(col_names).issubset(set(self.df.columns)) and (
                    not force_overwrite):
                merge_strategy = 'skip'
                logger.debug(('Skipping df merge as it looks like the merge has'
                               ' already taken place. To bypass this check and'
                               ' merge set force_overwrite = True'
                              ))                 
            else:
                merge_strategy = 'slice'
            logger.debug('Merging dataframe with the same index')
        elif obj_index_names == self.get_index_names():
            logger.debug('Merging dataframe with the same index names')
            merge_strategy = 'outer'
        elif len(obj_index_names) == 1:
            logger.debug('Merging a dataframe with single index key')
            merge_strategy = 'lookup'
            # validate index for lookup
            df_valid_names = set(self.get_index_names())
            df_valid_names.update(set(self.df.columns))        
            if not set(obj_index_names).issubset(df_valid_names):
                raise ValueError(('Function error.'
                              ' Attempting to merge a dataframe that has an'
                              ' invalid name in the index %s'
                              %(set(obj_index_names) - df_valid_names)))
                
        #carry out merge operation based on chosen merge strategy
        if merge_strategy == 'skip':
            # Add a null column for anything that should have been delivered
            missing_cols = [x for x in col_names if x not in self.df.columns]            
            for c in missing_cols:
                self.df[c] = None
        elif merge_strategy == 'replace':
            self.df = df
            self.apply_constants()
        elif merge_strategy == 'slice':
            for c in list(df.columns):
                self.df[c] = df[c]
        elif merge_strategy == 'outer':
            self.df = self.df.merge(df,'outer',
                          left_index=True,
                          right_index=True,
                          suffixes = ('',self.r_suffix))
            self.df = self.coalesce_cols(self.df,suffix= self.r_suffix)
            # A full outer join can add rows to the dataframe
            # Apply the constants to fill in the values of these new rows
            self.apply_constants()  
        elif merge_strategy == 'lookup':
            try:
                df_index_names = self.get_index_names()
                self.df = self.df.reset_index()
                self.df = self.df.merge(df,'left',
                          on = df.index.name,
                          suffixes = ('',self.r_suffix))
            except Exception:
                logger.error(
                        ('Function error when attempting to auto merge'
                         ' a dataframe. The merge object is not '
                         ' a slice; or another dataframe with a'
                         ' compatible index; or a lookup with a '
                         ' single index that matches on the the source'
                         ' columns. Modify the index of the merge object'
                         ' to get it to automerge or explicityly merge'
                         ' inside the function and return a merged '
                         ' result.' )
                        )
                raise       
            else:
                self.df = self.df.set_index(df_index_names)
                self.df = self.coalesce_cols(self.df,suffix= self.r_suffix)                
        else:
            logger.debug('Function error. Could not auto merge')
            if len(obj_index_names) == 0:
                raise ValueError(('Function error.'
                              'Attempting to merge a dataframe that has'
                              ' an un-named index. Set the index name.'
                              ' Index name/s may include any of the following'
                              ' columns: %s' 
                              %(df_valid_names)))     
            raise ValueError((
                    'Function error.'
                    ' Auto merge encountered a dataframe that could not'
                    ' be automatically merged.'                    
                    ' The most likely reason for this is an invalid index'
                    ' When returning a dataframe from a function, the index'
                    ' names should either match the index of the dataframe that'
                    ' was provided as input, or should be a single lookup key.'
                    ' When using a lookup key, it must exist in the columns'
                    ' or index of the input dataframe'
                    ' Output dataframe index is %s.'
                    ' Input dataframe index is %s.'
                    ' Input dataframe columns are %s.'
                    %(obj_index_names,
                      self.get_index_names(),
                      list(self.df.columns))
                    )) 
                
    def merge_non_dataframe(self,obj,col_names):
        '''
        Merge a non-dataframe object into the DataMerge dataframe object.
        '''
        if len(col_names)==1:
            # if the source dataframe is empty, it has no index
            # the data merge object can only accept a constant
            if len(df.index)==0:
                self.add_constant(col_names[0],obj)
            else:
                try:
                    self.df[col_names[0]] = obj
                except ValueError:
                    raise ValueError(
                        ('Auto merge encounterd an object %s that could'
                         ' not be automatically merged. Auto merge works best'
                         ' when supplied with time series data indexed the'
                         ' same way as subject of the merge or not time '
                         ' series data with a single part index that is '
                         ' readily identifyable as a source column' %obj
                        ))
        else:
            raise ValueError((
                    'Auto merge encountered an object %s that could not'
                    ' be automatically merged. When the object is not a'
                    ' dataframe or numpy array, it should only deliver a'
                    ' single column. This merge operation has columns '
                    ' %s' %(obj,col_names)
                    ))    
            

class DropNull(object):
    '''
    System function that drops null data
    '''
    
    is_system_function = True
    produces_output_items = False
    requires_input_items = False
    name = 'drop_null'
    
    def __init__(self,exclude_cols=None):
        
        if exclude_cols is None:
            exclude_cols = []
        self.exclude_cols = exclude_cols
        
    def execute(self,df):

        msg = 'columns excluded when dropping null rows %s' %self.exclude_cols
        logger.debug(msg)
        subset = [x for x in df.columns if x not in self.exclude_cols]
        msg = 'columns considered when dropping null rows %s' %subset
        logger.debug(msg)
        for col in subset:
            count = df[col].count()
            msg = '%s count not null: %s' %(col,count)
            logger.debug(msg)
        df = df.dropna(how='all', subset = subset )

        return df        
        
    
class DataWriterFile(object):
    '''
    Default data write stage. Writes to the file system.
    '''
    
    is_system_function = True
    requires_input_items = False
    produces_output_items = False
    
    def __init__(self,name,**params):
        
        self.name = name
        self.set_params(**params)
        
    def execute(self,df=None,start_ts=None,end_ts=None,entities=None):
                
        filename = 'data_writer_%s.csv' %self.name
        df.to_csv(filename)
        logger.debug('Wrote data to filename %s',filename)
        return df
    
    def set_params(self, **params):
        '''
        Set parameters based using supplied dictionary
        '''
        for key,value in list(params.items()):
            setattr(self, key, value)
        return self    
    
class DataReader(object):
    '''
    Default data reader stage. Calls get_data method on the object.
    '''
    is_system_function = True
    is_data_source = True
    requires_input_items = False
    #will be added by job controller
    _projection_list = None
    
    def __init__(self,name, obj):
        
        self.name = name
        self.obj = obj
        
    def execute(self,df=None,start_ts = None,end_ts=None,entities=None):
        
        return self.obj.get_data(start_ts = start_ts,
                                 end_ts=end_ts,
                                 entities=entities,
                                 columns = self._projection_list)
        
        
    def get_input_set(self):
        
        return set()
        
    def get_output_list(self):
        
        if not self._projection_list is None:
            outputs = self._projection_list
            logger.debug(('The get_data() method of the payload will return'
                          ' data items %s using a projection list set by'
                          ' the job controller'),outputs)
            
        else:                
            try:
                outputs = self.obj.get_output_items()
            except AttributeError:
                raise StageException(
                        ('The payload is missing a mandatory method'
                         ' get_data_items_list() returns a list of'
                         ' data items delivered by the get_data method'
                         ' of the payload. If the get_data method of the'
                         ' payload was not supposed to be called, set the'
                         ' payloads _auto_read_from_ts_table property to False'
                         ),self)
            else:
                logger.debug(('The payload has candidate'
              ' data items %s. The DataReader has no projection list'),outputs)
            
        if len(outputs) == 0:
            raise StageException(
                ('The data reader get_data_items_list() methor returned no'
                 ' data items'
                 ),self)               
            
            
        return outputs
    
class DataWriterException(Exception):
    
    def __init__(self, msg):
        super().__init__(msg)

class Db2DataWriter():
    '''
    Stage that writes the calculated data items to database.
    '''
    is_system_function = True
    MAX_NUMBER_OF_ROWS_FOR_SQL = 5000
    produces_output_items = False

    def __init__(self, name, data_item_metadata, db_connection, schema_name, grains_metadata, **kwargs):
        self.name = name
        self.data_item_metadata = data_item_metadata
        self.db_connection = db_connection
        self.schema_name = schema_name
        self.grains_metadata = grains_metadata
        self.kwargs = kwargs

        self.col_props = None
        self.table_props = None
        self.insert_statements = dict()
        self.row_lists = dict()

    def execute(self, df=None, start_ts=None, end_ts=None, entities=None):

        self.col_props = None
        self.table_props = None
        self.insert_statements = dict()
        self.row_lists = dict()

        if df is not None:
            logger.debug('Data items will be written to database for interval (%s, %s)' % (str(start_ts), str(end_ts)))

            try:
                # Delete old data item values in database and prepare insert statements
                self._delete_and_prepare(df, start_ts, end_ts)

                if len(self.col_props) > 0:
                    # Execute insert statements
                    self._persist_dataframe(df)
                else:
                    logger.warning('There are no data items that have to be written to the database.')
            finally:
                for table_name, stmt_insert in self.insert_statements.items():
                    ibm_db.free_result(stmt_insert)
        else:
            raise DataWriterException('The data frame is None.')

        return df

    def _delete_and_prepare(self, df, start_ts, end_ts):

        self.col_props = self._get_active_cols_properties(df)
        logger.info('The following data items will be written to the database: %s' %
                    (', '.join([('%s (%s, %s)' % (item_name, table_name, type))
                                for item_name, (type, table_name) in self.col_props.items()])))

        self.table_props = self._get_table_properties(df, self.col_props)
        logger.info('The data items will be written into the following tables: %s' %
                    (', '.join([table_name for table_name, dummy in self.table_props.items()])))

        for table_name, (sql_delete, sql_insert, index_pos) in self.table_props.items():

            # Delete old data items in database (Prepare, execute and free delete statement)
            try:
                stmt_delete = ibm_db.prepare(self.db_connection, sql_delete)
            except Exception as exc:
                raise DataWriterException('Preparation of the delete statement for table %s failed: %s' %
                                          (table_name, str(exc))) from exc

            try:
                logger.debug('Deleting old data items from table %s' % table_name)
                ibm_db.execute(stmt_delete, (str(start_ts), str(end_ts)))
                logger.info('%d old data items have been deleted from table %s' %
                                (ibm_db.num_rows(stmt_delete), table_name))
            except Exception as exc:
                raise DataWriterException('Execution of the delete statement for table %s failed: %s' %
                                          (table_name, str(exc))) from exc
            finally:
                if stmt_delete is not False:
                    ibm_db.free_result(stmt_delete)

            # Prepare insert statements
            try:
                stmt_insert = ibm_db.prepare(self.db_connection, sql_insert)
            except Exception as exc:
                raise DataWriterException('Preparation of the insert statement for table %s failed: %s' %
                                          (table_name, str(exc))) from exc

            self.insert_statements[table_name] = stmt_insert

            # Create one result list per table
            self.row_lists[table_name] = list()

    def _persist_dataframe(self, df):

        counter = 0
        row = list()
        # Loop over rows of dataframe, loop over data item in rows
        for df_row in df.itertuples():
            for item_name, (item_type, table_name) in self.col_props.items():
                derived_value = getattr(df_row, item_name)
                if pd.isna(derived_value):
                    continue

                sql_delete, sql_insert, index_pos = self.table_props[table_name]
                ix = getattr(df_row, 'Index')

                # Collect data for new row in output table
                row.clear()
                row.append(item_name)
                for position in index_pos:
                    row.append(ix[position])

                if item_type == DATA_ITEM_TYPE_BOOLEAN:
                    row.append(1 if (bool(derived_value) is True) else 0)
                else:
                    row.append(None)

                if item_type == DATA_ITEM_TYPE_NUMBER:
                    myFloat = float(derived_value)
                    row.append(myFloat if np.isfinite(myFloat) else None)
                else:
                    row.append(None)

                if item_type == DATA_ITEM_TYPE_LITERAL:
                    row.append(str(derived_value))
                else:
                    row.append(None)

                if item_type == DATA_ITEM_TYPE_TIMESTAMP:
                    row.append(derived_value)
                else:
                    row.append(None)

                # Add new row to the corresponding row list
                row_list = self.row_lists[table_name]
                row_list.append(tuple(row))

                # Write data to database when we have reached the max number per bulk
                if len(row_list) >= Db2DataWriter.MAX_NUMBER_OF_ROWS_FOR_SQL:
                    saved_rows = self._persist_tuples(table_name, self.insert_statements[table_name], tuple(row_list))
                    counter += saved_rows
                    logger.info('Number of data item values persisted so far: %d (%s)' % (counter, table_name))
                    row_list.clear()

        # Write remaining data (final bulk for each table)) to database
        for table_name, row_list in self.row_lists.items():
            if len(row_list) > 0:
                saved_rows = self._persist_tuples(table_name, self.insert_statements[table_name], tuple(row_list))
                counter += saved_rows
                logger.info('Number of data item values persisted so far: %d (%s)' % (counter, table_name))
                row_list.clear()
        logger.info('Total number of persisted data item values: %d' % counter)

    def _persist_tuples(self, table_name, stmt_insert, row_tuples):
        try:
            numb_rows = ibm_db.execute_many(stmt_insert, row_tuples)
        except Exception as exc:
            raise DataWriterException('Persisting data item values to table %s failed: %s' %
                                      (table_name, str(exc))) from exc

        return numb_rows

    def _get_active_cols_properties(self, df):
        '''
        Return a dict with all columns(=data items) that are relevant for data persistence.
        Dict's value holds the corresponding data type and table name.

        Sort out all columns of data frame that
        1) do not correspond to a data item or
        2) do correspond to a transient data item
        3) have an inconsistent definition (table name or type of the corresponding data item is missing)
        '''
        col_props = dict()
        for col_name, col_type in df.dtypes.iteritems():
            metadata = self.data_item_metadata.get(col_name)
            if metadata is not None:
                if metadata.get(DATA_ITEM_TRANSIENT_KEY) is False:
                    table_name = metadata.get(DATA_ITEM_SOURCETABLE_KEY)
                    data_item_type = metadata.get(DATA_ITEM_COLUMN_TYPE_KEY)
                    if table_name is None:
                        logger.warning(
                            'No table name defined for data item ' + col_name +
                            '. The data item will not been written to the database.')
                    elif data_item_type is None:
                        logger.warning(
                            'No data type defined for data item ' + col_name +
                            '. The data item will not been written to the database.')
                    else:
                        if (data_item_type != DATA_ITEM_TYPE_BOOLEAN and
                                data_item_type != DATA_ITEM_TYPE_NUMBER and
                                data_item_type != DATA_ITEM_TYPE_LITERAL and
                                data_item_type != DATA_ITEM_TYPE_TIMESTAMP):
                            logger.warning(('Data item %s has the unknown type %s. The data item will be written ' +
                                           'as %s into the database') % (col_name, data_item_type,
                                                                        DATA_ITEM_TYPE_LITERAL))
                            data_item_type = DATA_ITEM_TYPE_LITERAL
                        col_props[col_name] = (data_item_type, table_name)
                else:
                    logger.info(
                        'Data item ' + col_name + ' is not written to database because it is marked as transient.')
            else:
                logger.info('The column ' + col_name + ' in data frame does not correspond to a data item.')

        return col_props

    def _get_table_properties(self, df, col_props):

        # Set up a map for the relation index name and index position
        map_index_name_pos = {name: pos for pos, name in enumerate(df.index.names)}
        logger.debug(
            'Mapping between index name and index position: %s' %
            (', '.join([('%s -> %d' % (name, pos)) for name, pos in map_index_name_pos.items()])))

        # Assemble the sql statements and the required index elements for each table referenced in col_props
        table_props = dict()
        for item_name, (type, table_name) in col_props.items():
            table_prop = table_props.get(table_name)
            if table_prop is None:
                grain = self.grains_metadata.get(item_name)
                sql_delete = self.create_delete_statement(table_name)
                sql_insert = self.create_insert_statement(table_name, grain)
                logger.debug(
                    'For table %s: delete statement: %s insert statement: %s' % (table_name, sql_delete, sql_insert))

                index_positions = list()
                if not isinstance(df.index, pd.MultiIndex):
                    # only one element in the grain, ix is not an array, just append it anyway
                    index_positions = None
                elif grain is None:
                    # no grain, the index must be an array of (id, timestamp)
                    index_positions.append(0)
                    index_positions.append(1)
                else:
                    if grain.entity_id is not None:
                        # entity_first, the first level index must be the entity id
                        index_positions.append(0)
                    if grain.freq is not None:
                        if grain.entity_id is not None:
                            # if both id and time are included in the grain, time must be at pos 1
                            index_positions.append(1)
                        else:
                            # if only time is included, time must be at pos 0
                            index_positions.append(0)
                    if grain.dimensions is not None:
                        for dimension in grain.dimensions:
                            index_positions.append(map_index_name_pos[dimension])
                logger.debug('For table %s: Index elements are at positions: %s' %
                             (table_name, ', '.join([str(pos) for pos in index_positions])))

                table_props[table_name] = (sql_delete, sql_insert, index_positions)

        return table_props

    def create_insert_statement(self, table_name, grain):
        dimensions = []
        if grain is None:
            dimensions.append(KPI_ENTITY_ID_COLUMN)
            dimensions.append(KPI_TIMESTAMP_COLUMN)
        else:
            if grain.entity_id is not None:
                dimensions.append(KPI_ENTITY_ID_COLUMN)
            if grain.freq is not None:
                dimensions.append(KPI_TIMESTAMP_COLUMN)
            if grain.dimensions is not None:
                dimensions.extend(grain.dimensions)

        colExtension = ''
        parmExtension = ''

        for dimension in dimensions:
            quoted_dimension = dbhelper.quotingColumnName(dimension)
            colExtension += ', ' + quoted_dimension
            parmExtension += ', ?'

        stmt = ('INSERT INTO %s.%s (KEY%s, VALUE_B, VALUE_N, VALUE_S, VALUE_T, LAST_UPDATE) ' +
                'VALUES (?%s, ?, ?, ?, ?, CURRENT TIMESTAMP)') % \
               (dbhelper.quotingSchemaName(self.schema_name),
                dbhelper.quotingTableName(table_name),
                colExtension,
                parmExtension)

        return stmt

    def create_delete_statement(self, table_name):
        stmt = ('DELETE FROM %s.%s' %
                (dbhelper.quotingSchemaName(self.schema_name), dbhelper.quotingTableName(table_name)))
        where1 = ('%s >= %s' % (KPI_TIMESTAMP_COLUMN, ' ? '))
        where2 = ('%s < %s' % (KPI_TIMESTAMP_COLUMN, ' ? '))
        stmt = '%s WHERE %s AND %s' % (stmt, where1, where2)

        return stmt

        
class JobController(object):
    '''
    Job controllers manage the execution of a payload. The payload may have
    different modes of execution operating on different schedules. When the 
    payload executes it may retrieve data. The amount of historical data
    retrieved is governed by the backtrack property. Each different schedule
    may have a different setting for backtracking.
    
    The job controller interacts with the payload to determine the schedule
    and backtracking settings. The job controller has the ability to execute
    the payload in separate chunks to reduce memory consumption.
    
    The job controller manages persistence of a job log that keeps track of
    completed executions.
    
    See the TextPayload class to understand the standard interfaces that 
    any Payload that  is managed by the JobController should support. Many of
    the payload properties are optional. When the method is not present on
    the payload, the job controller will process the payload using defaults.
    
    
    Parameters
    ----------
    payload : object
        Any object that conforms to the payload API can be executed by a 
        JobController. The job control assumes has one or more "stage" to be
        executed. A "stage" is an object with an "execute" method.
        
        The payload will generally by an iotfunctions EntityType.
        
    **kwargs:
        The contents of the kwargs dictionary will be added to the payload so
        that the stage objects that execute as part of the payload have access 
        to them.
        
        e.g. a kwarg named "is_training_mode" is supplied to the JobController 
        object. The payload is an EntityType. The JobController uses the 
        set_param() method on the EntityType set the parameter.
        
        Functions executed have access to this parameter as follows:
        >>> entity_type = self.get_entity_type()
        >>> if entity_type.is_training_mode:
        >>>     #do something special
    
    '''
    # tupple has freq round hour,round minute, backtrack
    default_schedule = ('5min',None,None,None)
    default_chunk_size = '7d'
    default_is_schedule_progressive = True
    keep_alive_duration = None #'2min'
    recursion_limit = 99
    log_save_retries = [1,1,1,5,10,30,60,300]
    # Most of the work performed when executing a job is done by
    # executing the execute method of one or more stages defined in
    # the payload. There are however certain default classes that
    # come with the Job Controller that perform the work of 
    # aggegregating data, writing data and merging the results of 
    # the execution of a stage with data produced from prior stages
    data_aggregator = DataAggregator
    data_writer = DataWriterFile
    data_merge = DataMerge
    
    def __init__ (self,payload,**kwargs):
        
        self.payload = payload
        name = self.get_payload_param('name',None)
        if name is None:
            name = 'anonymous_payload_%s' %(payload.__class__.__name__)
            self.payload.name = name
        self.name = name
        # kwargs can be used to override default job controller and payload
        # parameters. All kwargs will be copied onto both objects.
        self.set_params(**kwargs)
        kwargs['save_trace_to_file'] = self.save_trace_to_file
        self.set_payload_params(**kwargs)
        
        #create a job log
        self.job_log = JobLog(self)
        self.stage_metadata = self.get_payload_param('_stages',None)
        
        if self.stage_metadata is None:
            raise ValueError((
                    'The playload for this job does not have valid metadata.'
                    ' To execute a payload using the JobController, the '
                    ' payload must have a "_stages" property that'
                    ' returns an appropriate dict containing all of the'
                    ' metadata for the job stages that will be executed. '
                    ))
        # Assemble a collection of candidate schedules to execute
        # If the payload does not have a schedule use the default
        schedules = self.get_payload_param('_schedules_dict',{})
        self._schedules = self.build_schedules_list(schedules)
        if self.default_schedule != self._schedules[0]:    
            self.default_schedule = self._schedules[0]
            logger.debug(('Changed default schedule to %s as a higher'
                          ' frequency schedule was present in the '
                          ' payload. You can set schedules on each data source'
                          ' explicitly to overide the default schedule'),
                          self.default_schedule[0])
        
    def __str__(self):
        
        out = '\n'
        out += 'Default schedule %s \n' %self.default_schedule[0]
        for (freq,start_hour,start_min,backtrack_days) in self._schedules :
            out += '    Schedule %s start %s:%s backtracks: %s \n' %(freq,
                                start_hour,start_min,backtrack_days)
        for key,value in list(self.stage_metadata.items()):
            out += 'Stages of type: %s at grain %s: \n' %(key[0],key[1])
            for v in value:
                out += '   %s on schedule %s requires %s produces %s\n' %(
                        v.name,v._schedule,
                        v._input_set,v._output_list )
        out += '\n'
        
        return out
    
    def adjust_to_start_date(self,execute_date,start_hours,start_min,interval):
        '''
        Adjust an execution date to conform to a schedule.
        Schedule has a start hour and start minute and interval
        Adjusted execution data cannot be in the future
        '''
        
        if start_hours is None and start_min is None:
            adjusted = execute_date
        else:
            if start_hours is None:
                start_hours = 0
            if start_min is None:
                start_min = 0
                
            execute_day = dt.datetime.combine(
                    execute_date.utcnow().date(),
                    dt.datetime.min.time())
            scheduled =  (execute_day +  dt.timedelta(hours=start_hours) 
                            + dt.timedelta(minutes=start_min))
            if scheduled > execute_date:
                scheduled = scheduled - dt.timedelta(days=1)
                
            interval = freq_to_timedelta(interval)
            periods = (execute_date - scheduled)//interval
            adjusted = scheduled + periods * interval

        return adjusted    
        
    def build_job_spec (self,schedule,subsumed):
        '''
        A job spec contains a list of stages to be executed as part of a job.
        The job spec is built according to the contents of the payload. The job
        controller builds the job spec on the fly by working out which job stages
        need for the current shedule and what that means  in terms of 
        dependencies. Consider the payload as a master template of 
        possible stages. A job spec contains the specific stages that are
        required for each execution.
        '''
    
        job_spec = OrderedDict()
        logger.debug(('Building a job spec for schedule %s with'
                      ' subsumbed schedules %s'),schedule,subsumed)
        
        build_metadata = {
                'spec' : [],
                'schedule' : schedule,
                'subsumed' : subsumed,
                'available_columns': set(),
                'required_inputs' : set(),
                'data_source_projection_list' : {}
                }
        
        # Retrieve and process input level data
        # Add stages that will be used to retrieve data
        build_metadata = self.build_stages_of_type(stage_type= 'get_data',
                                                    granularity = None,
                                                    meta = build_metadata)
        
        #Add a system function to remove null rows
        if self.get_payload_param('_drop_all_null_rows',False):
            drop_null_class = self.get_payload_param('drop_null_class',DropNull)
            
            exclude_cols = self.get_payload_param('_system_columns',[])
            custom_exclude = self.get_payload_param(
                            '_custom_exclude_col_from_auto_drop_nulls',[])
            exclude_cols.extend(custom_exclude)
            null_remover = drop_null_class(
                    exclude_cols = exclude_cols
                    )
            build_metadata['spec'].append(null_remover)  
        
        # Add transform stages to spec
        build_metadata = self.build_stages_of_type(stage_type= 'transform',
                                                    granularity = None,
                                                    meta = build_metadata)
        
        data_items_dict = {}
        for d in self.get_payload_param('_data_items',None):
            data_items_dict[d['name']] = d
        
        # Add a data write to spec
        params = {
            'db_connection' : self.get_payload_param('db',None).connection,
            'schema_name' : self.get_payload_param('_db_schema',None),
            'grains_metadata' : self.get_payload_param('_granularities_dict',None),
            'data_item_metadata' : data_items_dict
            }
        
        data_writer = self.data_writer(name = 'data_writer_input_level_',
                                       **params)
        build_metadata['spec'].append(data_writer)
        
        # build of input level is complete
        job_spec['input_level'] = build_metadata['spec']
        
        # Aggregation
        input_level_items = build_metadata['available_columns']
        for g in self.get_granularities():
            logger.debug('Building job spec for aggregation to grain: %s', g.name)
            build_metadata['spec'] = []
            build_metadata['available_colums'] = input_level_items
            
            # Simple aggregates are collapsed together for performance
            # The agg_dict is a pandas aggregate_dictionary keyed by column 
            # with a list of aggregation rules
            result = self.collapse_aggregation_stages(
                    granularity=g,
                    available_columns = build_metadata['available_colums'])
            (agg_dict, complex_aggregators, collapsed_stages, inputs, outputs) = result
            build_metadata['required_inputs'] |= set(inputs)
            build_metadata['available_colums'] |= set(outputs)
            
            logger.debug(('Collapsed aggregation stages %s down to a single'),
                         [x.name for x in collapsed_stages]
                          )
            
            # The job controller uses a generic DataAggregator to perform simple 
            # aggregations using an agg_dict and complex aggregators using apply
            aggregate_stage = self.data_aggregator(
                    name= 'auto_aggregate',
                    granularity = g,
                    agg_dict = agg_dict,
                    complex_aggregators = complex_aggregators,
                    input_items = inputs,
                    output_items = outputs
                    )
            build_metadata['spec'].append(aggregate_stage)
            build_metadata['available_columns'] |= set(outputs)
            logger.debug('Added aggregregator to job spec: %s', aggregate_stage)
       
            # Add transform stages for grain to job_spec 
            build_metadata = self.build_stages_of_type(stage_type= 'transform',
                                                    granularity = g,
                                                    meta = build_metadata)
            
            # Add a data writer for grain
            data_writer = self.data_writer(name = 'data_writer_ouput_%s' %g.name,
                                       **params)
            build_metadata['spec'].append(data_writer)         
            
            logger.debug('Completed job spec build for grain: %s', g.name )
            job_spec[g.name] = build_metadata['spec']
            
        
        #Trim data sources to retieve only the data items required as inputs
        for stage,cols in list(build_metadata['data_source_projection_list'].items()):
            required_cols = set(build_metadata['required_inputs'])
            # The payload may designate that certain columns are not allowed to be
            # trimmed
            required_cols |= set(self.get_payload_param('_mandatory_columns',[]))
            required_cols = set(cols).intersection(required_cols)
            logger.debug((
                    'Evaluating data source %s. Data items required from this'
                    ' source for this execution are %s'), stage.name,required_cols
                    )
            if len(required_cols) == 0:
                logger.debug(('Data source %s is not required for this'
                              ' execution as none of its data items used.'),
                                stage.name)
                job_spec = self.remove_stage(job_spec,stage)
            elif len(required_cols) != len(cols):
                logger.debug(('Trimming data source %s down to columns %s as'
                              ' remaining columns %s are not used'),
                              stage.name, required_cols,
                              set(cols)-required_cols )
                required_cols = list(required_cols)
                self.set_stage_param(stage,'_projection_list',required_cols)
                self.set_stage_param(stage,'_output_list',required_cols)
        
        logger.debug('Build of job spec is complete.')
        for key,value in list(job_spec.items()):
            logger.debug('Job spec: %s has stages %s',
                         key, [x.name for x in value])
            
        print('TBD - Add stages for usage stats and write to MessageHub')
            
        return job_spec
    
    def build_schedules_list(self,schedules_dict):
        '''
        Returns a sorted list of tuples containing 
        (freq,start_hour,start_min,backtrack_days)
        '''        
        #combine default with other schedules
        
        if schedules_dict is None:
            schedules_dict = {}
        
        if self.default_schedule[0] not in schedules_dict:
            schedules_dict[self.default_schedule[0]] = (
                    self.default_schedule[1], self.default_schedule[2],
                    self.default_schedule[3])
            
        #sort frequencies by duration
        freq_list = list(schedules_dict.keys())
        #sort freq_list
        sort_dict = {}
        for f in freq_list:
            sort_dict[freq_to_timedelta(f)] = f
        durations = list(sort_dict.keys())
        durations.sort()                
        #asseble output list
        schedules = []
        for duration in durations:
            freq = sort_dict[duration]
            start_hour,start_min,backtrack_days = schedules_dict[freq]
            schedules.append((freq,start_hour,start_min,backtrack_days))
            
        return schedules    
        
    
    def build_stages_of_type( self,
                              stage_type,
                              granularity,
                              meta):
    
        '''
        Add stages of a type on a schedule to a build spec contained within
        a metadata dictionary.
        
        Stages may have dependencies between them. Process recursively to find
        the stages that can be included based on the currently available
        Columns. Expand the currently available columns each time a new stage 
        is added.
        
        Return a metadata dictionary that incudes the current build spec and 
        various pieces of metadata around columns processed so far.
        
        '''
        
        for i in range(self.recursion_limit):
            result = self.gather_available_stages(
                            stage_type = stage_type,
                            schedule=meta['schedule'],
                            subsumed=meta['subsumed'],
                            available_columns=meta['available_columns'],
                            prev_stages=meta['spec'],
                            granularity=granularity) 
            (stages_added,columns_added,required_inputs,data_source_col_list) = result
            meta['spec'].extend(stages_added)
            meta['available_columns'] |= columns_added
            meta['required_inputs'] |= required_inputs
            if len(stages_added) == 0:
                break
            else:
                logger.debug('Added stages of type %s: %s', stage_type, 
                             [x.name for x in stages_added])
                logger.debug('New stages provide data items %s', columns_added)
                logger.debug('New stages require inputs %s', required_inputs)
            #maintain a set of cols for each data source stage
            for stage,cols in data_source_col_list.items():
                existing_cols = meta['data_source_projection_list'].get(stage,set())
                cols = set(cols)
                cols |= set(existing_cols)
                meta['data_source_projection_list'][stage] = cols
                
                
        #log which stages and data items were skipped
        (all_stages,all_cols) = self.get_stages(stage_type=stage_type,
                                                granularity=granularity,
                                                available_columns = None,
                                                exclude_stages = [])
        logger.debug('Built stages of type %s',stage_type)
        logger.debug('Available columns: %s', meta['available_columns']) 
        skipped = [x.name for x in set(all_stages)-set(meta['spec'])]
        for s in skipped:
            logger.debug('Skipped stage %s',skipped)
            logger.debug('Skipped data items %s',
                         self.exec_stage_method(
                            s,
                            'get_data_item_list',
                            'No data items'))
        return meta
    
    def build_trace_name(self,execute_date):
        
        if execute_date is None:
            execute_date = dt.datetime.utcnow()
        execute_str = f'{execute_date:%Y%m%d%H%M%S%f}' 
    
        return '%s_%s_trace_%s' %(self.payload.__class__.__name__,
                               self.name,execute_str)
    
    def collapse_aggregation_stages(self,granularity, available_columns):
        '''
        Collapse multiple simple aggregation stages down to an agg dict
        containing a list of aggregation functions to be applied by column name
        
        Returns the aggregate dictionary, a list of complex aggregation functions 
        and a set of inputs and list of outputs
    
        '''
        agg_dict = {}
        inputs = set()
        outputs = []
        all_stages = []
        
        #simple aggregators
        stages,cols = self.get_stages(stage_type='simple_aggregate',
                                         granularity = granularity,
                                         available_columns = available_columns,
                                         exclude_stages = []) 
        all_stages.extend(stages)
        for s in stages:
            input_items = list(s._input_set)
            if len(input_items) != 1:
                raise ValueError((
                        'A simple aggregator must take a single input item.'
                        ' %s has items: %s' %s.name,input_items
                        ))
            output_items = s._output_list
            if len(output_items) != 1:
                raise ValueError((
                        'A simple aggregator must produce a single output '
                        ' %s has items: %s' %s.name,output_items))            
            # aggregation is performed using a the pandas agg function
            # the aggregation function is either a string that is understood 
            # by pandas or a method that accepts a series
            # and returns a constant. 
            aggregation_method = self.get_stage_param(s,'_agg_function',s.execute)
            try:
                agg_dict[input_items[0]].append(aggregation_method)
            except KeyError:
                agg_dict[input_items[0]] = [aggregation_method]
            inputs.add(input_items[0])
            outputs.append(output_items[0])
            
        #complex aggregators
        complex_aggregators,cols = self.get_stages(stage_type='complex_aggregate',
                                         granularity = granularity,
                                         available_columns = available_columns,
                                         exclude_stages = [])
        
        
        all_stages.extend(complex_aggregators)
        for s in complex_aggregators:
            inputs |= s._input_set
            outputs.extend(s._output_list)
                
        return (agg_dict,complex_aggregators,all_stages,inputs,outputs)
    
    
    def df_concat(self,df1,df2):
        '''
        Concatenate two dataframes
        '''
        df = pd.concat([df1,df2])
        return df        
        
    def execute(self):
        '''
        Call the execute method on the payload object. If the payload has 
        multiple schedules decide which of them should be executed on this run.
        If data must be processed in chunks, deteremine the start and end date
        of the chunks and execute each chunk. Adjust the start date of each
        chunk to match a calendar period boundary if the payload indicates that
        this is neccessary.
        '''
        
        execute_date = dt.datetime.utcnow()
        if self.keep_alive_duration is not None:
            execute_until = execute_date + freq_to_timedelta(self.keep_alive_duration)
            logger.debug((
                    'Job will continue executing until %s as it has a keep'
                    'alive duration of %s'),
                    execute_until,self.keep_alive_duration
                    )
        else:
            execute_until = execute_date

        EngineLogging.finish_setup_log()

        # process continuously until execute_until date is reached
        # after time is up, job will be end. An external scheduler will create
        # a new one to replace it.
        # catlog code changes are recognised during execution
        
        execution_counter = 0
        constants = {}
        while execute_date <= execute_until:

            EngineLogging.start_run_log(self.payload.tenant_id, self.payload.name)
            logger.debug ((
                    'Starting execution number: %s with execution date: %s'),
                    execution_counter, execute_date
                    )
            
            
            # evalute the all candidate schedules that were indentified when
            # the job controller was initialized. 
            # The resulting dictionary contains a dictionary of status items
            # about each schedule
            try:
                schedule_metadata = self.evaluate_schedules(execute_date)
            except BaseException as e:
                meta = {'execution_date':execute_date,
                        'previous_execution_date': None,
                        'next_future_execution': None}
                #can't recover from errors evaluating the schedule 
                self.handle_failed_start(
                        meta,
                        exception=e,
                        stage_name = 'evaluate_schedules',
                        raise_error = True, #force an error to be raised
                        **meta
                        )
                
            can_proceed = True
            # look for schedules that were flagged 'is_due'.
            # These will be executed.
            
            for (schedule,meta) in list(schedule_metadata.items()):
                
                if not meta['is_due']:
                    try:
                        self.log_schedule_not_due(schedule=schedule,
                                               schedule_metadata = meta)
                    except BaseException as e:
                        logger.warning('Error logging non-execution data: %s',e)
                        
                    can_proceed = False
                        
                if can_proceed:
                    self.log_start(
                       meta,
                       status='running')
                    try:
                        (preload_stages,cols) = self.get_stages(
                                            stage_type = 'preload',
                                            granularity = None,
                                            available_columns = set(),
                                            exclude_stages = [])
                    except BaseException as e:
                        msg = 'Aborted execution. Error getting preload stages'
                        self.handle_failed_execution(
                                meta,
                                message = msg,
                                exception = e,
                                stage_name = 'get_stages("preload")',
                                raise_error = False
                                )
                        #preload stages are considered critical to successful
                        #execution. If a preload stage is optional, handle the
                        #error inside the preload stage
                        can_proceed = False
                        
                    # the output of a preload stage is a boolean column
                    # until we retrieve data, it has nowhere to go, 
                    # for now we will declare it as a constant
                    
                if can_proceed and len(preload_stages) != 0:
                    
                    logger.debug('Executing preload stages:')
                    try:
                        (df,can_proceed) = self.execute_stages(preload_stages,
                                            start_ts=meta['prev_checkpoint'],
                                            end_ts=execute_date,
                                            df=None)
                    except BaseException as e:
                        msg = 'Aborted execution. Error getting preload stages'
                        can_proceed = self.handle_failed_execution(
                            meta,
                            message = msg,
                            exception = e,
                            stage_name = 'execute_stages("preload")'        
                            )
                        df = None
                        can_proceed = False
                        
                    else:
                        
                        for c in cols:
                            constants[c] =True
                        logger.debug('Preload stages complete')
                        
                if can_proceed:
                        
                    # build a job specification
                    try:
                        job_spec = self.build_job_spec(
                                schedule=schedule,
                                subsumed=meta['mark_complete'])
                    except BaseException as e:
                        self.handle_failed_execution(
                                meta,
                                message = 'Failed when building job spec',
                                exception = e,
                                raise_error = False,
                                stage_name = 'build_job_spec)'        
                                )
                        can_proceed = False                       
                    
                    
                if can_proceed:
                    # divide up the date range to be processed into chunks
                    try:
                        chunks = self.get_chunks(
                                    start_date=meta['start_date'],
                                    end_date=execute_date,
                                    round_hour = meta['round_hour'],
                                    round_min = meta['round_min'],
                                    schedule = schedule)
                    except BaseException as e:
                         self.handle_failed_execution(
                                meta,
                                message = 'Error identifying chunks',
                                exception = e,
                                raise_error = False,
                                stage_name = 'get_chunks)'        
                                )
                         can_proceed = False
                         chunks = []
                
                    
                for i,(chunk_start,chunk_end) in enumerate(chunks):
                            
                    # execute the job spec for each chunk.
                    # add the constants that were produced by
                    # the preload stages
                    
                    if can_proceed:

                        kwargs = {'chunk' : i,
                                  'start_date':chunk_start,
                                  'end_date': chunk_end}                                  

                        if len(chunks) > 1 :
                            self.trace_add('Processing in chunks',
                                          log_method = logger.debug,
                                          **kwargs)
                        else: 
                            self.trace_add('Processing as a single chunk',
                                          log_method = logger.debug,
                                          **kwargs)
                                                
                        # execute input level stages
                        
                        try:
                            (df,can_proceed) = self.execute_stages(
                                    stages = job_spec['input_level'],
                                    start_ts=chunk_start,
                                    end_ts=chunk_end,
                                    df=None,
                                    constants = constants)
                        except BaseException as e:
                             self.handle_failed_execution(
                                    meta,
                                    message = 'Error executing stage',
                                    exception = e,
                                    raise_error = False
                                    )
                             can_proceed = False
                             
                                                            
                    for (grain,stages) in list(job_spec.items()):
                        
                        if can_proceed and grain != 'input_level':
                            
                                try:
                                    (result,can_proceed) = self.execute_stages(
                                            stages = stages,
                                            start_ts=chunk_start,
                                            end_ts=chunk_end,
                                            df=df)
                                except BaseException as e:
                                     self.handle_failed_execution(
                                            meta,
                                            message = 'Error executing stage',
                                            exception = e,
                                            raise_error = False
                                            )
                                     can_proceed = False
                                        
                
                #write results of this execution to the log
                
                if can_proceed:
                    status = 'complete'
                else:
                    status = 'aborted'
                    
                try:
                    self.log_completion(metadata = meta,
                                    status = status)
                except BaseException as e:
                    # an error writing to the jo log could invalidate
                    #future runs. Abort on error.
                     self.handle_failed_execution(
                            meta,
                            message = 'Error writing execution results to log',
                            exception = e,
                            raise_error = True
                            ) 
            
            try:
                next_execution = self.get_next_future_execution(schedule_metadata)
            except BaseException as e:
                raise_error(exception=e,
                            msg = 'Error getting next future scheduled execution',
                            stageName = 'get_next_future_execution',
                            raise_error = True
                            )
            meta['next_future_execution'] = next_execution
            
            # if there is no future execution that fits withing the timeframe
            # of this job, no need to hang around and wait
            
            if next_execution is not None and next_execution < execute_until:
                self.sleep_until(next_execution)
            else:
                logger.debug((
                        'Ending job normally as there are no scheduled executions '
                        ' due before execution end time'
                            ))
                self.trace_end()
                break
                
            
            execution_counter += 1
            execute_date = dt.datetime.utcnow()
        
            
    def execute_stages(self,stages,df,start_ts,end_ts,constants=None):
        '''
        Execute a series of stages contained in a job spec. 
        Combine the execution results with the incoming dataframe.
        Return a new dataframe.
        
        When the execution of a stage fails or results in an empty
        dataframe, payload properties determine whether
        execution of remaining stages should go ahead or not. If 
        execution proceeds on failure of a stage, the columns that
        were supposed to be contributed by the stage will be set to null.
        
        '''
        
        #create a new data_merge object using the dataframe provided
        merge = self.data_merge(df=df,constants=constants)
        can_proceed = True
        
        for s in stages:

            #get stage processing metadata
            new_cols = self.exec_stage_method(s,'get_output_list',None)            
            produces_output_items  = self.get_stage_param(
                                            s,'produces_output_items',True)
            discard_prior_data = self.get_stage_param(
                                            s,'_discard_prior_on_merge',False)
            
            #build initial trace info
            tw = {'produces_output_items' : produces_output_items,
                  'output_items': new_cols,
                  'discard_prior_data' : discard_prior_data }            
            
            ssg = ' Completed stage.'
            
            if not can_proceed:
                ssg = ''
                self.trace_add(
                        msg= 'Skipping stage %s.' %s.name,
                        created_by = s,
                        log_method = logger.debug,
                        **tw
                        )
                
            else:
        
                self.trace_add(
                        msg= 'Executing stage %s.' %s.name,
                        created_by = s,
                        log_method = logger.debug,
                        **tw
                        )
                
                #halt execution if no data 
                if not self.get_stage_param(s,'_allow_empty_df',True) and (
                        merge.df is None or len(df.index)==0):
                    can_proceed = False
                    ssg = ( ' Unable to execute stage.'
                            ' Function received an empty dataframe as'
                            ' input. Processing will halt as this function'
                            ' is configured to only run when there is data'
                            ' to process'
                            )
                    
            if can_proceed:
                #execute stage and handle errors
                
                try:
                
                    result = self.execute_stage(stage=s,
                                            df=merge.df,
                                            start_ts=start_ts,
                                            end_ts=end_ts)
                except BaseException as e:
                    
                    (can_proceed,df) = self.handle_failed_stage(
                            stage = s,
                            exception = e,
                            df = result,
                            status='aborted',
                            raise_error = False,
                            **tw)
                    
                    merge.df = df
                                    
                    
            if can_proceed:
                    
                #combine result with data from prior stages
    
                if discard_prior_data:
                    tw['merge_result'] = 'replaced prior data'
                    merge.df = result
                
                elif produces_output_items:
                    if new_cols is None or len(new_cols) ==0:
                        msg = (
                                ' Function %s did not provide a list of columns produced'
                                ' when the get_output_list() method was called to'
                                ' inspect the stage prior to execution. This is a '
                                ' mandatory method. It should return a list with at '
                                ' least one data item name. (%s).'  %(s.name, new_cols)
                                )
                        raise StageException(msg,s.name)
                    
                    #execute the merge
                    else:
                        try:
                            tw['merge_result'] = merge.execute(
                                    obj=result,
                                    col_names = new_cols)
                        except BaseException as e:
                            (can_proceed,df) = self.handle_failed_stage(
                                    exception = e,
                                    message = 'Merge error',
                                    stage = s,
                                    df = df,
                                    **tw)
                            merge.df = df
                            ssg = 'Error during merge'
                            
                else:
                    tw['new_data_items_info'] = ('Function is configured not to'
                                                 ' produce any new data items '
                                                 ' during execution' )
             
            #Write results of execution to trace
            tw['can_proceed'] = can_proceed
            
            self.trace_update(
                    msg=ssg,
                    df = merge.df,
                    log_method=logger.info,
                    **tw)                   
            
            df = merge.df
            
        return (df, can_proceed)
    
    def execute_stage(self,stage,df,start_ts,end_ts):
        
        
        # There are a few possible outcomes when executing a stage
        # 1. You get a dataframe with data as expected
        # 2. You get an empty dataframe
        # 3. You get a boolean value. An explict False means halt processing.
        # 4. A boolean True will be treated as an empty dataframe
        
        # The payload may optionally supply a specific list of 
        # entities to retrieve data from
        entities = self.exec_payload_method('get_entity_filter',None)
        
        # There are two possible signatures for the execute method
        try:
            result = stage.execute(df=df,
                                   start_ts=start_ts,
                                   end_ts=end_ts,
                                   entities = entities)
        except TypeError:
            is_executed = False
        else:
            is_executed = True
        
        # This seems a bit long winded, but it done this way to avoid
        # the type error showing up in the stack trace when there is an
        # error executing
        if not is_executed:
            result = stage.execute(df=df)
        
        if isinstance(result,bool) and result:
            result = pd.DataFrame()
                
        return result     
    
        
    def exec_payload_method(self,method_name,default_output,**kwargs):
    
        try:
            return(getattr(self.payload,method_name)(**kwargs))
        except (TypeError,AttributeError):
            logger.debug(('Error attempting to execute method %s() on'
                          ' payload %s %s. This method does'
                          ' not appear to be present. Returning'
                          ' default value %s'),method_name,
                          type(self.payload.name),self.payload.name,
                          default_output)
            return(default_output)
            
    def exec_stage_method(self,stage,method_name,default_output,**kwargs):
    
        try:
            return(getattr(stage,method_name)(**kwargs))
        except (TypeError,AttributeError) as e:
            logger.debug(('No method %s on %s returning default %s. %s'),
                          method_name, stage.name, default_output , e)
            return(default_output)


    def evaluate_schedules(self,execute_date):
        '''
        Examine all of the job schedules and identify which are due to run. 
        Gather job control metadata and return a dict keyed by schedule
        containing a dict that indicates for each schedule, when it will next 
        run, if it is currently due, the start date for data extraction and 
        which other schedules should be marked complete at the end of execution.
        '''
        
        schedule = OrderedDict()
        last_schedule_due = None
        all_due = []
        for (s,round_hour,round_min,backtrack) in self._schedules:
            if backtrack is None:
                backtrack = self.get_payload_param('default_backtrack',None)
            meta = {}
            schedule[s] = meta
            meta['schedule'] = s
            meta['execution_date'] = execute_date
            result = self.get_next_execution_date(schedule=s,
                                                  current_execution_date=execute_date,
                                                  round_hour = round_hour,
                                                  round_min =  round_min)
            (meta['adjusted_exec_date'],meta['previous_execution_date']) = result
            meta['is_subsumed'] = False
            meta['prev_checkpoint'] = None
            meta['is_checkpoint_driven'] = False
            meta['round_hour'] = round_hour
            meta['round_min'] = round_min
            meta['schedule_start'] = '%s:%s' %(round_hour,round_min)
            if meta['adjusted_exec_date'] <= execute_date:
                meta['is_due'] = True
                meta['start_date'] = None
                meta['backtrack'] = backtrack
                if meta['backtrack'] == 'checkpoint':
                    meta['is_checkpoint_driven'] = True
                    #retrieve data since the last checkpoint
                    meta['prev_checkpoint'] = (
                            self.job_log.get_last_execution_date(
                                    name=self.name,schedule=s)
                            )
                    if meta['prev_checkpoint'] is not None:
                        meta['start_date']= (
                                meta['prev_checkpoint'] +
                                freq_to_timedelta('1us')
                                )
                elif meta['backtrack'] is not None:
                    meta['start_date'] = (
                            meta['adjusted_exec_date'] - 
                            freq_to_timedelta(meta['backtrack'])
                            )
                meta['mark_complete'] = [s]
                last_schedule_due = s
                all_due.append(s)
                next_future = meta['adjusted_exec_date'] + freq_to_timedelta(s)
                meta['next_future_execution'] = next_future
            else:
                #This schedule is not due
                meta['is_due'] = False
                meta['mark_complete'] = []
                meta['backtrack'] = None
                meta['next_future_execution'] = meta['adjusted_exec_date']
                
        #progressive schedules imply that the last schedule involves
        # doing the work of the prior schedules so there it is only
        # neccessary to execute the last. If the schedules are not
        # proggressive, they will be executed independently        
        
        if last_schedule_due is not None:
            is_schedule_progressive = (
                    self.get_payload_param(
                            'is_schedule_progressive',
                            self.default_is_schedule_progressive)
                    )
            if is_schedule_progressive:
                for s,meta in list(schedule.items()):
                    if s == last_schedule_due:
                        meta['mark_complete'] = all_due
                        logger.debug(
                            'Schedule %s will execute', last_schedule_due)
                        subsumed = [x for x in meta['mark_complete'] if x!=s]
                        if len(subsumed) > 0:
                            logger.debug('This schedules subsumes %s.', subsumed)
                    elif meta['is_due']:
                        meta['is_due'] = False
                        meta['is_subsumed'] = True
                
        return schedule


    def gather_available_stages(self,stage_type,
                                schedule,
                                subsumed,
                                available_columns,
                                prev_stages,
                                granularity=None):
        '''
        Assemble a list of new execution stages that match set of criteria
        for stage_type and available columns. Returns a tuple containing a
        list of new stages and a set of new columns added by these stages
        
        '''
        required_input_set = set()
        schedules = set([schedule])
        schedules |= set(subsumed)
        data_source_projection_list = {}
        # get candidate stages
        # candidate stages have not already been processed
        # candiate stages do not require any columns that have't been added yet
        (candidate_stages,new_cols) = self.get_stages(stage_type=stage_type,
                                             granularity = granularity,
                                             available_columns=available_columns,
                                             exclude_stages = prev_stages
                                             )
        for s in candidate_stages:
            #set the schedule
            if self.get_stage_param(s,'schedule',None) is None:
                self.set_stage_param(s,'schedule',self.default_schedule[0])
                
        stages = [s for s in candidate_stages if s.schedule in schedules]
        new_cols = set()
        for s in stages:
            added_cols = self.exec_stage_method(
                    stage = s,
                    method_name = 'get_output_list',
                    default_output = [])
            new_cols |= set(added_cols)
            required_input_set |= self.exec_stage_method(
                    stage = s,
                    method_name = 'get_input_set',
                    default_output = [])
            #data sources have projection lists that the job controller
            # needs to underdstand as later on it will trim projection lists
            # to match data required based on schedule
            if self.get_stage_param(s,'is_data_source',False):
                data_source_projection_list[s] = added_cols
            #custom calendars are set on the payload
            if self.get_stage_param(s,'is_custom_calendar',False):
                self.set_payload_param('_custom_calendar',s)
                logger.debug((
                        'Stage %s added itself as the custom calendar'
                        ' to the payload'),s.name
                        )

        #Any code running outside of the main loop above will run whether or not
        # there where stages found. This is rather obvious, but what is not is that
        # is that build_stages is called multiple times for the same type of stage
        # until there are no more stages remaining of that type. This means that
        # any code placed outside of main loop will execute multiple times per
        # stage type. This is why there is no logging at this level.        
        new_cols = new_cols - available_columns
        
        return stages,new_cols,required_input_set, data_source_projection_list       

        
    def get_chunks(self,
                   start_date,
                   end_date,
                   round_hour,
                   round_min,
                   schedule):
        '''
        Divide a single period of time for an execution into multiple chunks.
        Each chunk will be executed separately. Chunk size is a pandas 
        frequency string. Chunk size will be derived from the payload or 
        defaulted if payload cannot provide.
        '''

        chunks = []
        chunk_size = self.get_payload_param('chunk_size', 
                                            self.default_chunk_size)        
        
        if start_date is None:
            start_date = self.exec_payload_method('get_early_timestamp',
                                                  None)
            if start_date is not None:
                logger.debug(
                        'Early timestamp obtained from payload as %s'
                        , start_date)
            else:
                logger.debug((
                        'The payload does not have an get_early_timestamp'
                        ' method or the method did not retrieve an early'
                        ' timestamp. Data will be retrieved in a single '
                        'chunk'))
                chunks = [(None, end_date)]
        
        if len(chunks) == 0:            
            chunk_start = start_date
            chunk_start = self.adjust_to_start_date(
                                execute_date=chunk_start,
                                start_hours=round_hour,
                                start_min=round_min,
                                interval=schedule
                                )
            chunk_start = self.exec_payload_method(
                            'get_adjusted_start_date',
                            chunk_start,
                            **{'start_date' : chunk_start})
            chunk_end = chunk_start + freq_to_timedelta(chunk_size)
            chunk_end = min(chunk_end,end_date)
            logger.debug('First chunk will run %s to %s',
                         chunk_start, chunk_end)
            chunks.append((chunk_start,chunk_end))
            
            while chunk_end < end_date:    
                chunk_start = chunk_end + freq_to_timedelta('1us')
                chunk_start = self.exec_payload_method(
                                'get_adjusted_start_date',
                                chunk_start,
                                **{'start_date' : chunk_start})
                chunk_end = chunk_start + freq_to_timedelta(chunk_size)
                chunk_end = min(chunk_end,end_date)
                logger.debug('Next chunk will run %s to %s',chunk_start,
                             end_date)
                chunks.append((chunk_start,chunk_end))        
            
        return chunks            
            
    
    def get_granularities(self):
        '''
        Inspect the stage metadata to infer a set of granularities that are
        required. Granularites are unique collection of data_items that
        aggregates are grouped by.
        '''
        
        granularites = set()
        for (stage_type,granularity) in list(self.stage_metadata.keys()):
            if granularity is not None:
                granularites.add(granularity) 
        
        return granularites
            
    def get_next_execution_date(self,schedule,
                                current_execution_date,
                                round_hour= None,
                                round_min = None):
               
        '''
        Get the next scheduled execution date for a particular
        schedule for the current execution date
        '''
        
        last_execution_date = self.job_log.get_last_execution_date (
                                name = self.name,schedule = schedule
                                )
        if last_execution_date is None:
            next_execution = current_execution_date
        else:
            next_execution = last_execution_date + freq_to_timedelta(schedule)            
        logger.debug(
            'Last execution of schedule %s was %s. Next execution due %s.', 
             schedule, last_execution_date, 
             next_execution)

        next_execution = self.adjust_to_start_date(
                 execute_date = current_execution_date,
                 start_hours = round_hour,
                 start_min = round_min,
                 interval = schedule
                )
            
        return (next_execution, last_execution_date)
    
    
    def get_next_future_execution(self,schedule_metadata):
        
        '''
        Get the next execution date across all schedules
        '''
        
        next_future = None
        for meta in list(schedule_metadata.values()):
            if next_future is None or meta['next_future_execution'] < next_future:
                next_future = meta['next_future_execution']
                
        logger.debug('Next scheduled execution date is %s', next_future)
                
        return next_future
        
    
    def get_payload_param(self,param,default=None):
        
        '''
        Retrieve a parameter from the payload object. Return default value
        if payload does not have the parameter.
        '''
        
        try:
            out = getattr(self.payload,param)
        except AttributeError:
            out = default
        return out
    
    
    def get_stages(self,stage_type,granularity,available_columns,exclude_stages):
        '''
        Get stages of a particular type, with a specific granularity, that
        can be executed using a set of columns and exclude specific stages.
        
        If available_columns is set to None, stages will not be filtered by
        available columns
        '''
        
        stages = self.stage_metadata.get((stage_type,granularity),[])
        out = []
        cols = set()
        for s in stages:
            if s not in exclude_stages and (
                    available_columns is None or
                    len(s._input_set - available_columns) == 0):
                out.append(s)
                new_cols = set(s._output_list)
                if available_columns is not None:
                     new_cols = new_cols - available_columns    
                cols |= new_cols
        
        return (out,cols)    

    def get_stage_param(self,stage,param,default=None):
        '''
        Retrieve a parameter value from a particular stage. Return
        default provided if the stage does not have this parameter.
        '''
        try:
            out = getattr(stage,param)
        except AttributeError:
            '''
            logger.debug(('No %s property on %s using default %s'),
                          param, stage.name, default )
            '''
            out = default
        return out

    
    def log_completion(self,metadata,status='complete',retries=None,**kw):
        '''
        Log job completion
        '''
        
        if retries is None:
            retries = self.log_save_retries
        
        trace = self.get_payload_param('_trace',None)
        if trace is not None:
            tw = { 
                    'status': status,
                    'next_future_execution' : metadata['next_future_execution'],
                    'execution_date' : metadata['execution_date']
                 }
            tw = {**kw,**tw}
            trace.write(
                    created_by = self,
                    text = 'Execution completed',
                    **tw
                    )
            trace.save()
            
        failed_log_updates = []
                    
        for m in metadata['mark_complete']:
            wrote_log = False
            for i in retries:
                try:
                    self.job_log.update(name = self.name,
                                    schedule = m,
                                    execution_date = metadata['execution_date'],
                                    status = status,
                                    next_execution_date = metadata['next_future_execution']
                                    )
                except BaseException as e:
                    logger.warning(('Unable to write completed execution'
                                    ' status to the log. Will try again in'
                                    ' %s seconds' %i))
                    timer.sleep(i)
                else:
                    wrote_log = True
                    break
                
            if not wrote_log and status == 'complete':
                entry = {'schedule':m,
                         'name':self.name,
                         'execution_date':metadata['execution_date'],
                         'status': status,
                         next_execution_date : metadata['next_future_execution']
                         }
                failed_log_updates.append(entry)
                
        if failed_log_updates:
            logger.warning('***********************************************')
            logger.warning('Error writing completed job to the job log')
            logger.warning(entry)
            logger.warning('***********************************************')    
            raise RuntimeError('Failed to update completion status of job')
                
            
    def handle_failed_execution(self,
                            metadata,
                            exception,
                            status='aborted',
                            message = None,
                            stage_name = None,
                            startup_log = None,
                            execution_log = None,
                            raise_error = None,
                            **kw):
        '''
        Log an execution that failed to complete successfully
        Reflect in trace.
        Save the trace and stop autosave.
        Raise the error
        '''
        
        try:
            stage_name = exception.stageName
        except AttributeError:
            pass
        
        if stage_name is None:
            stage_name = self.name
            
        if message is None:
            message = 'Execution failed'
        if startup_log is None:
            startup_log = EngineLogging.get_setup_log_cos_path()
        if execution_log is None:
            execution_log = EngineLogging.get_current_run_log_cos_path()
            
        tw = {  'execution_date' : metadata['execution_date'],
                'next_future_execution' : metadata['next_future_execution'],
                'startup_log' : startup_log,
                'execution_log' : execution_log
            }
        tw = {**kw,**tw}

        trace = self.get_payload_param('_trace',None)
        
        if not trace is None:
            self.trace_error(
                exception = exception,
                created_by = self,
                msg = message,
                **tw
                    )
            trace.save()
            trace.stop()
                    
        for m in self._schedules:
            
            self.job_log.update(
                    name = self.name,
                    schedule = m[0],
                    execution_date = metadata['execution_date'],
                    status = status,
                    next_execution_date = metadata['next_future_execution']
                    )
            
        can_proceed = self.raise_error(exception=exception,
                         msg=message,
                         stageName=stage_name,
                         raise_error = raise_error)
        
        return can_proceed

    def handle_failed_stage(self,
                            stage,
                            exception,
                            df,
                            status='aborted',
                            message = None,
                            raise_error = None,
                            **kw):
        '''
        Handle the failure of a single stage
        Reflect in trace.
        Raise the error if the execution cannot continue
        Return a tuple containing a bool (can_continue) and a dataframe
        '''

        err_info = {
            'AttributeError' : 'The function makes reference to an object property that does not exist',
            'SyntaxError' : 'The function contains a syntax error. If the function includes a type-in expression, make sure this is correct',
            'ValueError' : 'The function is operating on a data that has an unexpected value for its data type',
            'TypeError' : 'The function is operating on a data that has an unexpected data type',
            'KeyError' : 'The function is refering to a dictionary key or dataframe column name that doesnt exist',
            'NameError' : 'The function is refering to an object that doesnt exist. If refering to data items in a pandas dataframe, ensure that you quote them, e.g. df["temperature"]',
        }
        
        if message is None:
            message = 'Execution of stage %s failed. ' %stage.name
            
        message = message + err_info.get(exception.__class__.__name__,'')
    
        trace = self.get_payload_param('_trace',None)
        if not trace is None:
            self.trace_error(
                exception = exception,
                created_by = stage,
                msg = message,
                **kw
                )
            
        can_proceed = self.raise_error(exception=exception,
                         msg=message,
                         stageName=stage.name,
                         raise_error = raise_error)
        
        if can_proceed and kw['produces_output_items'] and isinstance(df,pd.DataFrame):
            new_cols = kw['output_items']
            for c in new_cols:
                df[c] = None
            if trace is not None:
                kw['added_null_columns'] = new_cols 
                trace.update_last_entry(msg = message ,**kw)
            
        return (can_proceed,df)


    def handle_failed_start(self,metadata,
                            exception,
                            status='aborted',
                            message = None,
                            stage_name = None,
                            startup_log = None,
                            execution_log = None,
                            **kw):
        '''
        Log a job that was unable to start.
        Reflect in trace.
        Raise the error
        '''
        
        if stage_name is None:
            stage_name = self.name
        if message is None:
            message = 'Execution failed during startup'
        if startup_log is None:
            startup_log = EngineLogging.get_setup_log_cos_path()
        if execution_log is None:
            execution_log = EngineLogging.get_current_run_log_cos_path()
            
        tw = {  'execution_date' : metadata['execution_date'],
                'next_future_execution' : metadata['next_future_execution'],
                'startup_log' : startup_log,
                'execution_log' : execution_log
            }
        tw = {**kw,**tw}
        
        trace = self.get_payload_param('_trace',None)
        
        if not trace is None:
            self.trace_error(
                exception = exception,
                created_by = self,
                msg = message,
                **tw
                    )
            trace_name = trace.name
            trace.save()
            trace.stop()
        else:
            trace_name = None
                    
        for m in self._schedules:
            
            self.job_log.insert(name = self.name,
                    schedule = m[0],
                    execution_date = metadata['execution_date'],
                    previous_execution_date = metadata['previous_execution_date'],
                    next_execution_date =  metadata['next_future_execution'],
                    status = status,
                    startup_log = startup_log,
                    execution_log = execution_log,
                    trace = trace_name)
            
        self.raise_error(exception=exception,
                         msg=message,
                         stageName=stage_name)
            
        
    def log_schedule_not_due(self,schedule,schedule_metadata):
        '''
        Describe why schedule was skipped
        '''
        
        if schedule_metadata['is_subsumed']:
            logger.debug((
                    'Schedule %s skipped as the job controller is using a'
                    ' progressive schedule and this schedule is subsumed by'
                    ' another.') , schedule
                    )
        else:                         
            logger.debug((
            'Hang tight. Schedule %s is only due for execution on %s.') 
            , schedule, 
            schedule_metadata['adjusted_exec_date']
            )
                

    def log_start(self,metadata,
                  status='running',
                  startup_log = None,
                  execution_log = None):
        '''
        Log the start of a job. Reset the trace.
        '''
        
        if startup_log is None:
            startup_log = EngineLogging.get_setup_log_cos_path()
        if execution_log is None:
            execution_log = EngineLogging.get_current_run_log_cos_path()
        
        tm={
            'execution_date' : metadata['execution_date'],
            'schedule' : metadata['schedule'],
            'included_schedules' : metadata['mark_complete'],
            'previous_successful_execution' : metadata['previous_execution_date'],
            'is_checkpoint_driven' : metadata['is_checkpoint_driven'],
            'schedule_start' : metadata['schedule_start'],
            'backtrack_days' : metadata['backtrack'],
            'next_future_execution' : metadata['next_future_execution']
            }
        
        if metadata['backtrack'] is None:
            tm['backtrack_info'] = ('The backtrack setting for this execution'
                                    ' is null. The job retrieves all available'
                                    ' data on each execution. To change this'
                                    ' behavior, explicitly set period of time'
                                    ' to backtrack or set the entity constant'
                                    ' default_backtrack to "checkpoint" to'
                                    ' retrieve data from the last checkpoint.')

        elif metadata['backtrack'] == 'checkpoint':
            tm['backtrack_info'] = ('The backtrack setting for this execution'
                                    ' is "checkpoint". This means that only data'
                                    ' inserted since the last checkpoint will be'
                                    ' will be processed. If you need to '
                                    ' retrieve historical data, set the '
                                    ' backtrack property on the schedule or'
                                    ' set the entity_type constant '
                                    ' default_backtrack to None to retrieve'
                                    ' all data')

        else:
            tm['backtrack_info'] = ('The backtrack setting for this execution'
                                    ' is set to a specific number of days. '
                                    ' The number of days was determined by'
                                    ' taking the longest number of days from'
                                    ' this schedule and others that were '
                                    ' subsumed by it.'
                                    ' Backtrack behavior can also be set'
                                    ' using the entity type constant '
                                    ' default_backtrack.'
                                    ' Use a null value to retrieve all data'
                                    ' with each execution or the specify the'
                                    ' value of "checkpoint" to retrieve data'
                                    ' inserted since the last checkpoint' )
            
        if metadata['adjusted_exec_date'] != metadata['execution_date']:
            tm['adjusted_start_date'] = ('The start date for this execution '
                                        ' was adjusted to match the explicit'
                                        ' start hour and minute definined'
                                        ' for the schedule' )
        
        if metadata['is_checkpoint_driven'] and metadata['prev_checkpoint'] is None:
            tm['checkpoint_info'] = ('No previous checkpoint.'
                                      ' All data will be retrieved')
        
        trace = self.get_payload_param('_trace',None)
        if trace is None:
            trace_name = None
        else:
            trace_name = self.build_trace_name(metadata['execution_date'])
            trace.reset(
                    name=trace_name,
                    auto_save= self.get_payload_param('_auto_save_trace',None)
                    )
            trace.write(
                    created_by = self,
                    text = 'Started job',
                    log_method=None,
                    **tm
                    )
                    
        for m in metadata['mark_complete']:
            self.job_log.clear_old_running(name=self.name,schedule=m)
            self.job_log.insert(name = self.name,
                                schedule = m,
                                execution_date = metadata['execution_date'],
                                previous_execution_date = metadata['previous_execution_date'],
                                next_execution_date =  metadata['next_future_execution'],
                                status = status,
                                startup_log = startup_log,
                                execution_log = execution_log,
                                trace = trace_name)
                        
            
    def raise_error(self,exception,msg='',stageName=None, raise_error=None):
        '''
        Raise an exception, Include message and stage name.
        '''
        
        if raise_error is None:
            raise_error = self.get_payload_param('abort_on_fail',True)
        
        can_proceed = False
        
        msg = ('Execution of job failed. %s failed due to %s'
               ' Error message: %s '
               ' Stack trace : %s '
               ' Execution trace : %s'
               %(stageName,exception.__class__.__name__,msg,traceback.format_exc(),
                 str(self.get_payload_param('_trace','None provided')))
               )
         
        if raise_error:
            raise StageException(msg, stageName)
        else:
            logger.warning(msg)
            can_proceed = True
            
        return can_proceed
                      
    
    def remove_stage(self,job_spec,stage):
        '''
        Remove stage from a job spec
        '''
        
        for key,value in list(job_spec.items()):
            prev_value = value
            job_spec[key] = [x for x in value if x !=stage]
            if len(prev_value) != len(job_spec[key]):
                removed = (set([x.name for x in prev_value]) - 
                           set([x.name for x in job_spec[key]]))
                logger.debug('Removed stages: %s',removed)
        
        return job_spec
    
    def set_params(self, **params):
        '''
        Set parameters based using supplied dictionary
        '''
        for key,value in list(params.items()):
            setattr(self, key, value)
        return self

    def set_payload_params(self,**params):
        '''
        Add parameters to the payload
        
        '''
        
        for key,value in list(params.items()):
            setattr(self.payload, key, value)
            logger.debug('Setting param %s on payload to %s', key, value)
        return self.payload
    
    def set_payload_param(self,key,value):
        '''
        Set the value of a single parameter
        
        '''
        setattr(self.payload, key, value)
        return self.payload
    
    def set_stage_param(self,stage,param,value):
        '''
        Set the value of single parameter for a particular stage
        
        '''
        
        setattr(stage, param, value)
        return stage
    
    def sleep_until(self,next_execution):
        '''
        Pause execution until designated datetime value
        '''
        
        wait_for = 0
        if next_execution is not None:
            wait_for = next_execution-dt.datetime.utcnow()
            wait_for = wait_for.total_seconds()
        if wait_for > 0:
            logger.debug('Waiting %s seconds until next execution at %s',
                          wait_for, next_execution)
            time.sleep(wait_for)
    
    def trace_add(self,msg,created_by = None, log_method = None,df=None, **kwargs):
        '''
        Add a new trace entry to the payload
        '''
        if created_by is None:
            created_by = self

        if not self.get_payload_param('trace_df_changes',False):
            df = None            
        
        try:
            self.payload.trace_append(created_by=created_by,
                                      msg = msg,
                                      log_method=log_method,
                                      df = df,
                                      **kwargs)
        except AttributeError:
            logger.debug(('Payload has no trace_append() method.'
                          ' Trace will be written to log instead'))
            logger.debug('Trace:%s',msg)
            logger.debug('Payload:%s',kwargs)

    def trace_end(self):
        '''
        Stop the autosave thread on the trace
        '''       
        trace = self.get_payload_param('_trace',None)
        if trace is not None:
            try:
                trace.stop()
            except AttributeError:
                pass

    def trace_error(self,exception,
                    msg,
                    created_by = None,
                    log_method = logger.warning,
                    df = None,
                    **kwargs):
        '''
        Log the occurance of an error to the trace
        '''
        if created_by is None:
            created_by = self
            
        if not self.get_payload_param('trace_df_changes',False):
            df = None            
        
        error = {
                'exception_type' : exception.__class__.__name__,
                'exception': str(exception),
                'stack_trace' : traceback.format_exc()
                }
        
        kwargs = {**error,**kwargs}
        
        trace = self.get_payload_param('_trace',None)
        if trace is None:
            logger.debug(('Payload has no trace object. An error occured.'
                            ' Error will be written to the log instead'))
            logger.debug('Trace:%s',msg)
            logger.warning('Error:%s',error)            
        else:
            trace.write(
                    created_by=created_by,
                    text = msg,
                    log_method=log_method,
                    df = df,
                    **kwargs)
                
                
    def trace_update(self,msg, log_method = None, df = None, **kwargs):
        '''
        Update the most recent trace entry
        '''
        
        if not self.get_payload_param('trace_df_changes',False):
            df = None
        
        trace = self.get_payload_param('_trace',None)
        if trace is None:
            logger.debug('The payload has no trace object. Writing to log')
            logger.debug('message' ,msg)
            logger.debug('payload', kwargs)
        else:
            trace.update_last_entry(
                    msg = msg,
                    log_method = log_method,
                    df = df,
                    **kwargs)      
        
class JobLog(object):
    
    '''
    Create and manage a database table to store job execution history.
    
    A job log entry is created at the start of job execution with a default
    status of 'running'. This job log entry may be updated at various times
    during the process. Each update may update the status, trace or log 
    references.
    
    '''
    
    def __init__(self,job,table_name='job_log'):
        
        self.job = job
        self.table_name = table_name
        self.db = self.job.get_payload_param('db',None)
        if self.db is None:
            raise RuntimeError(('The job payload does not have a valid'
                                ' db object. Unable to establish a database'
                                ' connection'))
        kw = {
             'schema' : self.job.get_payload_param('_db_schema',None)
             }
        
        self.table = Table(self.table_name, self.db.metadata,
                Column('object_type', String(255)),
                Column('object_name', String(255)),
                Column('schedule', String(255)),
                Column('execution_date', DateTime()),
                Column('previous_execution_date',DateTime()),
                Column('next_execution_date',DateTime()),
                Column('status',String(30)),
                Column('startup_log',String(255)),
                Column('execution_log',String(255)),
                Column('trace',String(2000)),
                extend_existing = True,
                **kw
                )
        
        self.db.metadata.create_all(self.db.connection)

    def clear_old_running(self,
                          name,
                          schedule):
        
        q = self.table.select().\
            where(and_(
                self.table.c.object_type == self.job.payload.__class__.__name__,
                self.table.c.object_name == name,
                self.table.c.schedule == schedule,
                self.table.c.status == 'running'                  
                    ))
        df = pd.read_sql(sql=q,con=self.db.connection)
        
        if len(df.index)>0:
            upd = self.table.update().values(status='abandoned').\
            where(and_(
                self.table.c.object_type == self.job.payload.__class__.__name__,
                self.table.c.object_name == name,
                self.table.c.schedule == schedule,
                self.table.c.status == 'running'                  
                    ))
            
            self.db.connection.execute(upd)
            logger.debug(
                    'Marked existing running jobs as abandoned  (%s,%s)',
                    name,schedule
                    )
            logger.debug(df)
            self.db.commit()            
        

    def insert (self,name,schedule,execution_date,status='running',
                previous_execution_date = None, next_execution_date = None,
                startup_log=None,execution_log=None,trace=None):
        
        self.db.start_session()
        ins = self.table.insert().values(object_type = self.job.payload.__class__.__name__,
                               object_name = name,
                               schedule = schedule,
                               execution_date = execution_date,
                               previous_execution_date = previous_execution_date,
                               next_execution_date = next_execution_date,
                               status = status,
                               startup_log = startup_log,
                               execution_log = execution_log,
                               trace = trace
                               )
        self.db.connection.execute(ins)
        logger.debug((
                'Created job log entry (%s,%s): %s'),
                name,schedule,execution_date
                )
        self.db.commit()

        
    def update (self,name,schedule,execution_date, next_execution_date = None,
                status=None, execution_log=None,trace=None):
        
        self.db.start_session()
        
        values = {}
        if status is not None:
            values['status'] = status
        if execution_log is not None:
            values['execution_log'] = execution_log            
        if trace is not None:
            values['trace'] = trace
        if next_execution_date is not None:
            values['next_execution_date'] = next_execution_date
        if values:        
            upd = self.table.update().\
                where(and_(
                        self.table.c.object_type == self.job.payload.__class__.__name__,
                        self.table.c.object_name == name,
                        self.table.c.schedule == schedule,
                        self.table.c.execution_date == execution_date
                        )).\
                values(**values)
            
            self.db.connection.execute(upd)
            logger.debug((
                    'Updated job log (%s,%s): %s'),
                    name,schedule,execution_date
                    )
            self.db.commit()
        else:
            logger.debug((
                    'No non-null values supplied. job log was not updated (%s,%s): %s'),
                    name,schedule,execution_date
                    )                    
        
    def get_last_execution_date( self,name, schedule):
        
        '''
        Last execution date for payload object name for particular schedule
        '''
        
        col = func.max(self.table.c['execution_date'])
        query = select([col.label('last_execution')]).where(and_(
                self.table.c['object_type'] == self.job.payload.__class__.__name__,
                self.table.c['object_name'] == name,
                self.table.c['schedule'] == schedule,
                self.table.c['status'] == 'complete'
                ))
        result = self.db.connection.execute(query).first()
        
        return result[0]
    
class MergeError(Exception):
    
    def __init__(self, msg):
        super().__init__(msg)
            
    
class ExpressionExecutor(object):
    '''
    Create a new item from an expression involving other items
    '''
    def __init__(self, expression , name, input_items = None):
        self.expression = expression
        self.name = name
        if input_items is None:
            input_items = set()
        self._input_set = input_items
                
    def execute(self, df):
        self.infer_inputs(df)
        if '${' in self.expression:
            expr = re.sub(r"\$\{(\w+)\}", r"df['\1']", self.expression)
        else:
            expr = self.expression
        try:
            df[self.name] = eval(expr)
        except SyntaxError:
            msg = 'Syntax error while evaluating expression %s' %expr
            raise SyntaxError (msg)
        
        return df
    
    def infer_inputs(self,df):
        #get all quoted strings in expression
        possible_items = re.findall('"([^"]*)"', self.expression)
        possible_items.extend(re.findall("'([^']*)'", self.expression))
        self.input_items = [x for x in possible_items if x in list(df.columns)]          


class CalcPipeline:
    '''
    A CalcPipeline executes a series of dataframe transformation stages.
    '''
    def __init__(self,stages = None,entity_type =None):
        self.logger = logging.getLogger('%s.%s' % (self.__module__, self.__class__.__name__))
        self.entity_type = entity_type
        self.set_stages(stages)
        self.log_pipeline_stages()
        warnings.warn(
            "CalcPipeline is deprecated. Replaced by JobController.",
            DeprecationWarning )
        
    def add_expression(self,name,expression):
        '''
        Add a new stage using an expression
        '''
        stage = PipelineExpression(name=name,expression=expression,
                                   entity_type=self.entity_type)
        self.add_stage(stage)
        
    def add_stage(self,stage):
        '''
        Add a new stage to a pipeline. A stage is Transformer or Aggregator.
        '''
        stage.set_entity_type(self.entity_type)
        self.stages.append(stage)
          
        
    def _extract_preload_stages(self):
        '''
        pre-load stages are special stages that are processed outside of the pipeline
        they execute before loading data into the pipeline
        return tuple containing list of preload stages and list of other stages to be processed
        
        also extract scd lookups. Place them on the entity.
        '''
        stages = []
        extracted_stages = []
        for s in self.stages:
            try:
                is_preload = s.is_preload
            except AttributeError:
                is_preload = False
            #extract preload stages
            if is_preload:
                msg = 'Extracted preload stage %s from pipeline' %s.__class__.__name__
                logger.debug(msg)
                extracted_stages.append(s)
            else:
                stages.append(s)
                
        return (extracted_stages,stages)
                        
    
    def _execute_preload_stages(self, start_ts = None, end_ts = None, entities = None, register= False):
        '''
        Extract and run preload stages
        Return remaining stages to process
        '''
        (preload_stages,stages) = self._extract_preload_stages()
        preload_item_names = []
        #if no dataframe provided, querying the source entity to get one
        for p in preload_stages:
            if not self.entity_type._is_preload_complete:
                msg = 'Stage %s :' %p.__class__.__name__
                status = p.execute(df=None,start_ts=start_ts,end_ts=end_ts,entities=entities)
                msg = '%s completed as pre-load. ' %p.__class__.__name__
                self.trace_add(msg)
                if register:
                    p.register(df=None)
                try:
                    preload_item_names.append(p.output_item)
                except AttributeError:
                    msg = 'Preload functions are expected to have an argument and property called output_item. This preload function is not defined correctly'
                    raise AttributeError (msg)
                if not status:
                    msg = 'Preload stage %s returned with status of False. Aborting execution. ' %p.__class__.__name__
                    self.trace_add(msg)
                    stages = []
                    break
        self.entity_type._is_preload_complete = True
        return(stages,preload_item_names)
    
    
    def _execute_data_sources(self,stages,
                                df,
                                start_ts=None,
                                end_ts=None,
                                entities=None,
                                to_csv=False,
                                register=False,
                                dropna = False):
        '''
        Extract and execute data source stages with a merge_method of replace.
        Identify other data source stages that add rows of data to the pipeline
        '''
        remaining_stages = []
        secondary_sources = []
        special_lookup_stages = []
        replace_count = 0
        for s in stages:
            try:
                is_data_source =  s.is_data_source
                merge_method = s.merge_method
            except AttributeError:
                is_data_source = False
                merge_method = None        
                
            try:
                is_scd_lookup = s.is_scd_lookup
            except AttributeError:
                is_scd_lookup = False
            else:
                self.entity_type._add_scd_pipeline_stage(s)

            try:
                is_custom_calendar = s.is_custom_calendar
            except AttributeError:
                is_custom_calendar = False
            else:
                self.entity_type.set_custom_calendar(s)
                  
            if is_data_source and merge_method == 'replace':
                df = self._execute_stage(stage=s,
                    df = df,
                    start_ts = start_ts,
                    end_ts = end_ts,
                    entities = entities,
                    register = register,
                    to_csv = to_csv,
                    dropna = dropna,
                    abort_on_fail = True)
                msg = 'Replaced incoming dataframe with custom data source %s. ' %s.__class__.__name__
                self.trace_add(msg, df = df)
                
            elif is_data_source and merge_method == 'outer':
                '''
                A data source with a merge method of outer is considered a secondary source
                A secondary source can add rows of data to the pipeline.
                '''
                secondary_sources.append(s)
            elif is_scd_lookup or is_custom_calendar:
                special_lookup_stages.append(s)
            else:
                remaining_stages.append(s)
        if replace_count > 1:
            self.logger.warning("The pipeline has more than one custom source with a merge strategy of replace. The pipeline will only contain data from the last replacement")        
        
        #execute secondary data sources
        if len(secondary_sources) > 0:
            for s in secondary_sources:
                msg = 'Processing secondary data source %s. ' %s.__class__.__name__
                self.trace_add(msg)
                df = self._execute_stage(stage=s,
                    df = df,
                    start_ts = start_ts,
                    end_ts = end_ts,
                    entities = entities,
                    register = register,
                    to_csv = to_csv,
                    dropna = dropna,
                    abort_on_fail = True)
        
        #exceute special lookup stages
        if not df.empty and len(special_lookup_stages) > 0:                
            for s in special_lookup_stages:
                msg = 'Processing special lookup stage %s. ' %s.__class__.__name__
                self.trace_add(msg)
                df = self._execute_stage(stage=s,
                    df = df,
                    start_ts = start_ts,
                    end_ts = end_ts,
                    entities = entities,
                    register = register,
                    to_csv = to_csv,
                    dropna = dropna,
                    abort_on_fail = True) 
            
        return(df,remaining_stages)    
            
                
    def execute(self, df=None, to_csv=False, dropna=False, start_ts = None, end_ts = None, entities = None, preloaded_item_names=None,
                register = False):
        '''
        Execute the pipeline using an input dataframe as source.
        '''
        #preload may  have already taken place. if so pass the names of the items produced by stages that were executed prior to loading.
        if preloaded_item_names is None:
            preloaded_item_names = []
        msg = 'Executing pipeline with %s stages.' % len(self.stages)
        logger.debug(msg)            
        is_initial_transform = self.get_initial_transform_status()
        # A single execution can contain multiple CalcPipeline executions
        # An initial transform and one or more aggregation executions and post aggregation transforms
        # Behavior is different during initial transform
        if entities is None:
            entities = self.entity_type.get_entity_filter()
        start_ts_override = self.entity_type.get_start_ts_override()
        if start_ts_override is not None:
            start_ts = start_ts_override
        end_ts_override = self.entity_type.get_end_ts_override()            
        if end_ts_override is not None:
            end_ts = end_ts_override            
        if is_initial_transform:
            if not start_ts is None:
                msg = 'Start timestamp: %s.' % start_ts
                self.trace_add(msg)
            if not end_ts is None:
                msg = 'End timestamp: %s.' % end_ts
                self.trace_add(msg)                
            #process preload stages first if there are any
            (stages,preload_item_names) = self._execute_preload_stages(start_ts = start_ts, end_ts = end_ts, entities = entities,register=register)
            preloaded_item_names.extend(preload_item_names)
            if df is None:
                msg = 'No dataframe supplied for pipeline execution. Getting entity source data'
                logger.debug(msg)
                df = self.entity_type.get_data(start_ts=start_ts, end_ts = end_ts, entities = entities)            
            #Divide the pipeline into data retrieval stages and transformation stages. First look for
            #a primary data source. A primary data source will have a merge_method of 'replace'. This
            #implies that it replaces whatever data was fed into the pipeline as default entity data.
            (df,stages) = self._execute_data_sources (
                                                df = df,
                                                stages = stages,
                                                start_ts = start_ts,
                                                end_ts = end_ts,
                                                entities = entities,
                                                to_csv = to_csv,
                                                register = register,
                                                dropna =  dropna
                                                )
                          
        else:
            stages = []
            stages.extend(self.stages)
        if df is None:
            msg = 'Pipeline has no source dataframe'
            raise ValueError (msg)
        if to_csv:
            filename = 'debugPipelineSourceData.csv'
            df.to_csv(filename)
        if dropna:
            df = df.replace([np.inf, -np.inf], np.nan)
            df = df.dropna()
        # remove rows that contain all nulls ignore deviceid and timestamp
        if self.entity_type.get_param('_drop_all_null_rows'):
            exclude_cols = self.get_system_columns()
            exclude_cols.extend(self.entity_type.get_param('_custom_exclude_col_from_auto_drop_nulls'))
            msg = 'columns excluded when dropping null rows %s' %exclude_cols
            logger.debug(msg)
            subset = [x for x in df.columns if x not in exclude_cols]
            msg = 'columns considered when dropping null rows %s' %subset
            logger.debug(msg)
            for col in subset:
                count = df[col].count()
                msg = '%s count not null: %s' %(col,count)
                logger.debug(msg)
            df = df.dropna(how='all', subset = subset )
            self.log_df_info(df,'post drop all null rows')
        else:
            logger.debug('drop all null rows disabled')
        #add a dummy item to the dataframe for each preload stage
        #added as the ui expects each stage to contribute one or more output items
        for pl in preloaded_item_names:
            df[pl] = True
        for s in stages:
            if df.empty:
                self.logger.info('No data retrieved from all sources. Exiting pipeline execution')        
                break
                #skip this stage of it is not a secondary source             
            df = self._execute_stage(stage=s,
                                df = df,
                                start_ts = start_ts,
                                end_ts = end_ts,
                                entities = entities,
                                register = register,
                                to_csv = to_csv,
                                dropna = dropna,
                                abort_on_fail = True)
        if is_initial_transform:
            try:
                self.entity_type.write_unmatched_members(df)
            except Exception as e:
                msg = 'Error while writing unmatched members to dimension. See log.' 
                self.trace_add(msg,created_by = self)
                raise
                self.entity_type.raise_error(exception = e,abort_on_fail = False)
            self.mark_initial_transform_complete()

        return df
    
    def _execute_stage(self,stage,df,start_ts,end_ts,entities,register,to_csv,dropna, abort_on_fail): 
        try:
            abort_on_fail = stage._abort_on_fail
        except AttributeError:
            abort_on_fail = abort_on_fail
        try:
            name = stage.name
        except AttributeError:
            name = stage.__class__.__name__
        #check to see if incoming data has a conformed index, conform if needed
        try:
            df = stage.conform_index(df=df)
        except AttributeError:
            pass
        except KeyError as e:
            msg = 'KeyError while conforming index prior to execution of function %s. ' %name
            self.trace_add(msg,created_by = stage, df = df)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        #there are two signatures for the execute method
        msg = 'Stage %s :' % name
        self.trace_add(msg=msg,df=df)
        try:
            try:
                newdf = stage.execute(df=df,start_ts=start_ts,end_ts=end_ts,entities=entities)
            except TypeError:
                newdf = stage.execute(df=df)
        except AttributeError as e:
            self.trace_add('The function %s makes a reference to an object property that does not exist. ' %name,
                              created_by = stage)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        except SyntaxError as e:
            self.trace_add('The function %s contains a syntax error. If the function configuration includes a type-in expression, make sure that this expression is correct. ' %name,
                              created_by = stage)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        except (ValueError,TypeError) as e:
            self.trace_add('The function %s is operating on data that has an unexpected value or data type. ' %name,
                              created_by = stage)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        except NameError as e:
            self.trace_add('The function %s referred to an object that does not exist. You may be referring to data items in pandas expressions, ensure that you refer to them by name, ie: as a quoted string. ' %name,
                              created_by = stage)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        except BaseException as e:
            self.trace_add('The function %s failed to execute. ' %name, created_by = stage)
            self.entity_type.raise_error(exception = e,abort_on_fail = abort_on_fail,stageName = name)
        #validate that stage has not violated any pipeline processing rules
        try:
            self.validate_df(df,newdf)
        except AttributeError:
            msg = 'Function %s has no validate_df method. Skipping validation of the dataframe' %name
            logger.debug(msg)
        if register:
            try:
                stage.register(df=df,new_df= newdf)
            except AttributeError as e:
                msg = 'Could not export %s as it has no register() method or because an AttributeError was raised during execution' %name
                logger.warning(msg)
                logger.warning(str(e))
        if dropna:
            newdf = newdf.replace([np.inf, -np.inf], np.nan)
            newdf = newdf.dropna()
        if to_csv:
            newdf.to_csv('debugPipelineOut_%s.csv' %stage.__class__.__name__)

        msg = 'Completed stage %s. ' %name
        self.trace_add(msg,created_by=stage, df = newdf)
        return newdf 

    def get_custom_calendar(self):
        '''
        Get the optional custom calendar for the entity type
        '''
        return self.entity_type._custom_calendar
    
    def get_initial_transform_status(self):
        '''
        Determine whether initial transform stage is complete
        '''
        return self.entity_type._is_initial_transform    
    
    def get_input_items(self):
        '''
        Get the set of input items explicitly requested by each function
        Not all input items have to be specified as arguments to the function
        Some can be requested through this method
        '''
        inputs = set()
        for s in self.stages:
            try:
                inputs = inputs | s.get_input_items()
            except AttributeError:
                pass
            
        return inputs
    
    def get_scd_lookup_stages(self):
        '''
        Get the scd lookup stages for the entity type
        '''
        return self.entity_type._scd_stages
    
    def get_system_columns(self):
        '''
        Get a list of system columns for the entity type
        '''
        return self.entity_type._system_columns

    
    def log_df_info(self,df,msg,include_data=False):
        '''
        Log a debugging entry showing first row and index structure
        '''
        msg = log_df_info(df=df,msg=msg,include_data = include_data)
        return msg
    
    def log_pipeline_stages(self):
        '''
        log pipeline stage metadata
        '''
        msg = 'pipeline stages (initial_transform=%s) ' %self.entity_type._is_initial_transform
        for s in self.stages:
            msg = msg + s.__class__.__name__
            msg = msg + ' > '
        return msg
    
    def mark_initial_transform_complete(self):
        self.entity_type._is_initial_transform = False
        
    def publish(self):
        export = []
        for s in self.stages:
            if self.entity_type is None:
                source_name = None
            else:
                source_name = self.entity_type.name
            metadata  = { 
                    'name' : s.name ,
                    'args' : s._get_arg_metadata()
                    }
            export.append(metadata)
            
        response = self.entity_type.db.http_request(object_type = 'kpiFunctions',
                                        object_name = source_name,
                                        request = 'POST',
                                        payload = export)    
        return response
            
    
    
    def _raise_error(self,exception,msg, abort_on_fail = False):
        #kept this method to preserve compatibility when
        #moving raise_error to the EntityType
        self.entity_type().raise_error(
                exception = exception,
                msg = msg,
                abort_on_fail = abort_on_fail
                )

            
    def set_stages(self,stages):
        '''
        Replace existing stages with a new list of stages
        '''
        self.stages = []
        if not stages is None:
            if not isinstance(stages,list):
                stages = [stages]
            self.stages.extend(stages)
        for s in self.stages:
            try:
                s.set_entity_type(self.entity_type)
            except AttributeError:
                s._entity_type = self.entity_type
                
    def __str__(self):
        
        return self.__class__.__name__
            
    def trace_add(self,msg,created_by = None, log_method = None, **kwargs):
        '''
        Append to the trace information collected the entity type
        '''
        if created_by is None:
            created_by = self
        
        self.entity_type.trace_append(created_by=created_by,
                                      msg = msg,
                                      log_method=log_method,
                                      **kwargs)

    def validate_df(self, input_df, output_df):

        validation_result = {}
        validation_types = {}
        for (df, df_name) in [(input_df, 'input'), (output_df, 'output')]:
            validation_types[df_name] = {}
            for c in list(df.columns):
                try:
                    validation_types[df_name][df[c].dtype].add(c)
                except KeyError:
                    validation_types[df_name][df[c].dtype] = {c}

            validation_result[df_name] = {}
            validation_result[df_name]['row_count'] = len(df.index)
            validation_result[df_name]['columns'] = set(df.columns)
            is_str_0 = False
            try:
                if is_string_dtype(df.index.get_level_values(self.entity_type._df_index_entity_id)):
                    is_str_0 = True
            except KeyError:
                pass
            is_dt_1 = False
            try:
                if is_datetime64_any_dtype(df.index.get_level_values(self.entity_type._timestamp)):
                    is_dt_1 = True
            except KeyError:
                pass
            validation_result[df_name]['is_index_0_str'] = is_str_0
            validation_result[df_name]['is_index_1_datetime'] = is_dt_1

        if validation_result['input']['row_count'] == 0:
            logger.warning('Input dataframe has no rows of data')
        elif validation_result['output']['row_count'] == 0:
            logger.warning('Output dataframe has no rows of data')

        if not validation_result['input']['is_index_0_str']:
            logger.warning(
                'Input dataframe index does not conform. First part not a string called %s' % self.entity_type._df_index_entity_id)
        if not validation_result['output']['is_index_0_str']:
            logger.warning(
                'Output dataframe index does not conform. First part not a string called %s' % self.entity_type._df_index_entity_id)

        if not validation_result['input']['is_index_1_datetime']:
            logger.warning(
                'Input dataframe index does not conform. Second part not a string called %s' % self.entity_type._timestamp)
        if not validation_result['output']['is_index_1_datetime']:
            logger.warning(
                'Output dataframe index does not conform. Second part not a string called %s' % self.entity_type._timestamp)

        mismatched_type = False
        for dtype, cols in list(validation_types['input'].items()):
            try:
                missing = cols - validation_types['output'][dtype]
            except KeyError:
                mismatched_type = True
                msg = 'Output dataframe has no columns of type %s. Type has changed or column was dropped.' % dtype
            else:
                if len(missing) != 0:
                    msg = 'Output dataframe is missing columns %s of type %s. Either the type has changed or column was dropped' % (
                    missing, dtype)
                    mismatched_type = True
            if mismatched_type:
                logger.warning(msg)

        self.check_data_items_type(df=output_df, items=self.entity_type.get_data_items())

        return (validation_result, validation_types)

    def check_data_items_type(self, df, items):
        '''
        Check if dataframe columns type is equivalent to the data item that is defined in the metadata
        It checks the entire list of data items. Thus, depending where this code is executed, the dataframe might not be completed.
        An exception is generated if there are not incompatible types of matching items AND and flag throw_error is set to TRUE
        '''

        invalid_data_items = list()

        if df is not None:
            #logger.info('Dataframe types before type conciliation: \n')
            #logger.info(df.dtypes)

            for item in list(items.data_items):  # transform in list to iterate over it
                df_column = {}
                try:
                    data_item = items.get(item)  # back to the original dict to retrieve item object
                    df_column = df[data_item['name']]
                except KeyError:
                    #logger.debug('Data item %s is not part of the dataframe yet.' % item)
                    continue

                # check if it is Number
                if data_item['columnType'] == 'NUMBER':
                    if not is_numeric_dtype(df_column.values) or is_bool_dtype(df_column.dtype):
                        logger.info(
                            'Type is not consistent %s: df type is %s and data type is %s' % (
                                item, df_column.dtype.name, data_item['columnType']))

                        try:
                            df[data_item['name']] = df_column.astype('float64')  # try to convert to numeric
                        except Exception:
                            invalid_data_items.append((item, df_column.dtype.name, data_item['columnType']))
                    continue

                # check if it is String
                if data_item['columnType'] == 'LITERAL':
                    if not is_string_dtype(df_column.dtype):
                        logger.info(
                            'Type is not consistent %s: df type is %s and data type is %s' % (
                                item, df_column.dtype.name, data_item['columnType']))
                        try:
                            df[data_item['name']] = df_column.astype('str')  # try to convert to string
                        except Exception:
                            invalid_data_items.append((item, df_column.dtype.name, data_item['columnType']))
                    continue

                # check if it is Timestamp
                if data_item['columnType'] == 'TIMESTAMP':
                    if not is_datetime64_any_dtype(df_column.dtype):
                        logger.info(
                            'Type is not consistent %s: df type is %s and data type is %s' % (
                                item, df_column.dtype.name, data_item['columnType']))
                        try:
                            df[data_item['name']] = pd.to_datetime(df_column)  # try to convert to timestamp
                        except Exception:
                            invalid_data_items.append((item, df_column.dtype.name, data_item['columnType']))
                    continue

                # check if it is Boolean
                if data_item['columnType'] == 'BOOLEAN':
                    if not is_bool_dtype(df_column.dtype):
                        logger.info(
                            'Type is not consistent %s: df type is %s and data type is %s' % (
                                item, df_column.dtype.name, data_item['columnType']))
                        try:
                            df[data_item['name']] = df_column.astype('bool')
                        except Exception:
                            invalid_data_items.append((item, df_column.dtype.name, data_item['columnType']))
                    continue

        else:
            logger.info('Not possible to retrieve information from the data frame')

        if len(invalid_data_items) > 0:
            msg = 'Some data items could not have its type conciliated:'
            for item, df_type, data_type in invalid_data_items:
                msg += ('\n %s: df type is %s and data type is %s' % (item, df_type, data_type))
            logger.error(msg)
            raise Exception(msg)


class PipelineExpression(object):
    '''
    Create a new item from an expression involving other items
    '''
    def __init__(self, expression , name, entity_type):
        self.expression = expression
        self.name = name
        super().__init__()
        self.input_items = []
        self.entity_type = entity_type
                
    def execute(self, df):
        df = df.copy()
        self.infer_inputs(df)
        if '${' in self.expression:
            expr = re.sub(r"\$\{(\w+)\}", r"df['\1']", self.expression)
        else:
            expr = self.expression
        try:
            df[self.name] = eval(expr)
        except SyntaxError:
            msg = 'Syntax error while evaluating expression %s' %expr
            raise SyntaxError (msg)
        else:
            msg = 'Evaluated expression %s' %expr
            self.entity_type.trace_append(msg,df=df)
        return df

    def get_input_items(self):
        return self.input_items
    
    def infer_inputs(self,df):
        #get all quoted strings in expression
        possible_items = re.findall('"([^"]*)"', self.expression)
        possible_items.extend(re.findall("'([^']*)'", self.expression))
        self.input_items = [x for x in possible_items if x in list(df.columns)]       
            

