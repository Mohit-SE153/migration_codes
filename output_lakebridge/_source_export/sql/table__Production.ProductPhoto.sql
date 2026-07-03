CREATE TABLE [Production].[ProductPhoto] (
    [ProductPhotoID] int NOT NULL,
    [ThumbNailPhoto] varbinary(MAX) NULL,
    [ThumbnailPhotoFileName] nvarchar(50) NULL,
    [LargePhoto] varbinary(MAX) NULL,
    [LargePhotoFileName] nvarchar(50) NULL,
    [ModifiedDate] datetime NOT NULL
);
