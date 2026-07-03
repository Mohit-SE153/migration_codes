CREATE TABLE [Production].[ProductDescription] (
    [ProductDescriptionID] int NOT NULL,
    [Description] nvarchar(400) NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
